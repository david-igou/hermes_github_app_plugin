"""ghapp broker: policy-enforcing token minting behind a unix socket or TCP port.

Runs on the trusted side of the boundary (e.g. as a dedicated system user on
the Hermes VM, or as its own pod in a Kubernetes namespace). Holds the GitHub
App private key; agent-controlled processes only ever see single-repo,
permission-clamped installation tokens.

Design notes:
- Two transports, same HTTP surface. Unix socket (`--socket`): no TCP
  listener, no firewall surface, and possession of the socket (mount + group)
  is the client authorization. TCP (`--listen host:port`): for containerized
  deployments where clients are other pods and a socket cannot cross the pod
  boundary — there the client authorization is network reachability, so the
  listener MUST be scoped by the platform (NetworkPolicy / not exposed off
  the namespace). The policy engine below is the same either way and is the
  actual blast-radius bound.
- Requests exceeding policy are DENIED, not silently clamped — a clamped
  token would make agent failures confusing to debug.
- Every decision is one structured JSON line on stdout; under systemd that
  lands in journald (SYSLOG_IDENTIFIER=ghapp-broker).
"""

from __future__ import annotations

import fnmatch
import json
import socket
import socketserver
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import httpx
import yaml

from .auth import GitHubAppAuth, split_repo
from .config import ConfigurationError

_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}

# "none" is valid only in repo_max_permissions overrides: it removes a
# globally-allowed permission for the matching repos entirely.
_OVERRIDE_LEVELS = {"none": 0, **_PERMISSION_LEVELS}

_MAX_BODY_BYTES = 64 * 1024


class PolicyError(ConfigurationError):
    """Raised when the policy file is missing or invalid."""


@dataclass(frozen=True)
class Policy:
    """Broker minting policy."""

    allowed_repos: tuple[str, ...]
    max_permissions: dict[str, str]
    default_permissions: dict[str, str] = field(default_factory=dict)
    # OWNER/REPO fnmatch glob -> per-permission caps. Tightening only: an
    # override can lower or remove ("none") a globally-allowed permission for
    # matching repos, never add one. All matching globs apply; lowest wins.
    repo_max_permissions: dict[str, dict[str, str]] = field(default_factory=dict)

    def repo_allowed(self, repo: str) -> bool:
        return any(fnmatch.fnmatchcase(repo.lower(), pattern) for pattern in self.allowed_repos)

    def _effective_max(self, repo: str) -> dict[str, str]:
        effective = dict(self.max_permissions)
        lowered = repo.lower()
        for pattern, overrides in self.repo_max_permissions.items():
            if not fnmatch.fnmatchcase(lowered, pattern):
                continue
            for name, level in overrides.items():
                current = effective.get(name)
                if current is None:
                    continue
                if _OVERRIDE_LEVELS[level] < _OVERRIDE_LEVELS[current]:
                    effective[name] = level
        return effective

    def evaluate(
        self, repo: str, requested: dict[str, str] | None
    ) -> tuple[bool, str, dict[str, str]]:
        """Return (allowed, reason, effective_permissions)."""
        if not self.repo_allowed(repo):
            return False, f"repository {repo!r} is not in the broker policy", {}
        effective_max = self._effective_max(repo)
        if not requested:
            # Defaults are clamped (not denied) to the per-repo ceiling so a
            # plain read mint keeps working against capped repos.
            granted: dict[str, str] = {}
            for name, level in self.default_permissions.items():
                cap = effective_max.get(name)
                if cap is None or cap == "none":
                    continue
                if _PERMISSION_LEVELS.get(level, 99) > _PERMISSION_LEVELS[cap]:
                    granted[name] = cap
                else:
                    granted[name] = level
            return True, "granted default permissions", granted
        for name, level in requested.items():
            max_level = effective_max.get(name)
            if max_level is None:
                return False, f"permission {name!r} is not allowed by the broker policy", {}
            if max_level == "none":
                return (
                    False,
                    f"permission {name!r} is not allowed for {repo} by the broker policy",
                    {},
                )
            if _PERMISSION_LEVELS.get(level, 99) > _PERMISSION_LEVELS.get(max_level, 0):
                return (
                    False,
                    f"permission {name}={level} exceeds the policy maximum ({max_level})",
                    {},
                )
        return True, "granted requested permissions", dict(requested)


def _parse_repo_overrides(section: dict[str, Any]) -> dict[str, dict[str, str]]:
    overrides_raw = section.get("repo_max_permissions", {})
    if not isinstance(overrides_raw, dict):
        raise PolicyError("policy.repo_max_permissions must be a mapping of OWNER/REPO globs")
    repo_max_permissions: dict[str, dict[str, str]] = {}
    for pattern, perms in overrides_raw.items():
        if not isinstance(perms, dict) or not perms:
            raise PolicyError(f"policy.repo_max_permissions.{pattern} must be a non-empty mapping")
        entry = {str(k): str(v) for k, v in perms.items()}
        for name, level in entry.items():
            if level not in _OVERRIDE_LEVELS:
                raise PolicyError(
                    f"policy.repo_max_permissions.{pattern}.{name}: unknown level {level!r}"
                )
        repo_max_permissions[str(pattern).lower()] = entry
    return repo_max_permissions


def load_policy(path: Path) -> Policy:
    """Load and validate the policy file."""
    if not path.exists():
        raise PolicyError(f"policy file does not exist: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = raw.get("policy") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        raise PolicyError(f"policy file {path} must contain a top-level 'policy' mapping")

    repos_raw = section.get("allowed_repos")
    if not isinstance(repos_raw, list) or not repos_raw:
        raise PolicyError("policy.allowed_repos must be a non-empty list of OWNER/REPO globs")
    allowed_repos = tuple(str(r).lower() for r in repos_raw)

    max_raw = section.get("max_permissions")
    if not isinstance(max_raw, dict) or not max_raw:
        raise PolicyError("policy.max_permissions must be a non-empty mapping")
    max_permissions = {str(k): str(v) for k, v in max_raw.items()}
    for name, level in max_permissions.items():
        if level not in _PERMISSION_LEVELS:
            raise PolicyError(f"policy.max_permissions.{name}: unknown level {level!r}")

    defaults_raw = section.get("default_permissions", {"contents": "read"})
    if not isinstance(defaults_raw, dict) or not defaults_raw:
        raise PolicyError("policy.default_permissions must be a non-empty mapping")
    default_permissions = {str(k): str(v) for k, v in defaults_raw.items()}

    repo_max_permissions = _parse_repo_overrides(section)

    policy = Policy(
        allowed_repos=allowed_repos,
        max_permissions=max_permissions,
        default_permissions=default_permissions,
        repo_max_permissions=repo_max_permissions,
    )
    # Defaults must themselves satisfy the ceiling — fail at load, not at mint.
    ok, reason, _ = policy.evaluate(allowed_repos[0].replace("*", "x"), default_permissions)
    if not ok and "not in the broker policy" not in reason:
        raise PolicyError(f"policy.default_permissions exceed max_permissions: {reason}")
    return policy


def _audit(stream: Any, **fields: Any) -> None:
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "component": "ghapp-broker", **fields},
        sort_keys=True,
    )
    print(line, file=stream, flush=True)


class _BrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Threaded unix-socket HTTP server carrying broker state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        socket_path: str,
        handler: type[BaseHTTPRequestHandler],
        *,
        auth: GitHubAppAuth,
        policy: Policy,
        socket_mode: int,
        audit_stream: Any,
    ) -> None:
        self.auth = auth
        self.policy = policy
        self.audit_stream = audit_stream
        path = Path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        super().__init__(socket_path, handler)
        path.chmod(socket_mode)


class _TCPBrokerServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded TCP HTTP server carrying broker state (containerized mode)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        listen_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        auth: GitHubAppAuth,
        policy: Policy,
        audit_stream: Any,
    ) -> None:
        self.auth = auth
        self.policy = policy
        self.audit_stream = audit_stream
        super().__init__(listen_address, handler)


class _Handler(BaseHTTPRequestHandler):
    """Routes: GET /healthz, GET /status, POST /token."""

    server: _BrokerServer | _TCPBrokerServer
    protocol_version = "HTTP/1.1"

    # BaseHTTPRequestHandler logs to stderr per request; audit lines replace that.
    def log_message(self, format: str, *args: Any) -> None:
        return

    def address_string(self) -> str:
        # TCP connections have an (ip, port) client_address; unix sockets an
        # empty string.
        if isinstance(self.client_address, tuple) and self.client_address:
            return str(self.client_address[0])
        return "uds"

    def _peer(self) -> dict[str, Any]:
        if isinstance(self.client_address, tuple) and self.client_address:
            return {
                "peer_addr": str(self.client_address[0]),
                "peer_port": int(self.client_address[1]),
            }
        try:
            pid, uid, gid = struct.unpack(
                "3i",
                self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
            )
        except OSError:
            return {}
        return {"peer_pid": pid, "peer_uid": uid, "peer_gid": gid}

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, {"ok": True})
            return
        if self.path == "/status":
            config = self.server.auth.config
            self._respond(
                200,
                {
                    "client_id": config.client_id,
                    "app_slug": config.app_slug,
                    "installations": dict(config.installations),
                    "policy": {
                        "allowed_repos": list(self.server.policy.allowed_repos),
                        "max_permissions": dict(self.server.policy.max_permissions),
                        "default_permissions": dict(self.server.policy.default_permissions),
                    },
                },
            )
            return
        self._respond(404, {"error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/token":
            self._respond(404, {"error": f"unknown path {self.path}"})
            return
        peer = self._peer()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > _MAX_BODY_BYTES:
                raise ValueError(f"invalid Content-Length {length}")
            payload = json.loads(self.rfile.read(length))
            repo = str(payload["repo"])
            split_repo(repo)  # validate OWNER/REPO shape
            requested_raw = payload.get("permissions")
            requested = (
                {str(k): str(v) for k, v in requested_raw.items()}
                if isinstance(requested_raw, dict)
                else None
            )
            force_refresh = bool(payload.get("force_refresh", False))
        except (KeyError, ValueError, json.JSONDecodeError, ConfigurationError) as exc:
            self._audit_decision("denied", repo="?", reason=f"bad request: {exc}", peer=peer)
            self._respond(400, {"error": f"bad request: {exc}"})
            return

        allowed, reason, effective = self.server.policy.evaluate(repo.lower(), requested)
        if not allowed:
            self._audit_decision("denied", repo=repo, reason=reason, peer=peer, requested=requested)
            self._respond(403, {"error": reason})
            return

        try:
            token = self.server.auth.mint_for_repo(
                repo, effective or None, force_refresh=force_refresh
            )
        except (httpx.HTTPError, ConfigurationError) as exc:
            self._audit_decision(
                "error", repo=repo, reason=str(exc), peer=peer, requested=requested
            )
            self._respond(502, {"error": f"mint failed: {exc}"})
            return

        self._audit_decision(
            "granted",
            repo=repo,
            reason=reason,
            peer=peer,
            requested=requested,
            granted=dict(token.permissions),
            expires_at=token.expires_at.isoformat(),
        )
        self._respond(
            200,
            {
                "token": token.token,
                "expires_at": token.expires_at.isoformat(),
                "installation_id": token.installation_id,
                "client_id": token.client_id,
                "app_slug": token.app_slug,
                "owner": token.owner,
                "repositories": list(token.repositories),
                "permissions": dict(token.permissions),
            },
        )

    def _audit_decision(self, decision: str, *, peer: dict[str, Any], **fields: Any) -> None:
        _audit(self.server.audit_stream, event="token_request", decision=decision, **peer, **fields)


_MAX_PORT = 65535


def split_listen(listen: str) -> tuple[str, int]:
    """Parse a HOST:PORT listen address."""
    host, sep, port_raw = listen.rpartition(":")
    if not sep or not host or not port_raw.isdigit():
        raise ConfigurationError(f"invalid listen address {listen!r}; expected HOST:PORT")
    port = int(port_raw)
    if not 0 < port <= _MAX_PORT:
        raise ConfigurationError(f"invalid listen port {port}")
    return host, port


def serve(
    *,
    socket_path: str | None = None,
    listen: str | None = None,
    policy_path: str,
    auth: GitHubAppAuth,
    socket_mode: int = 0o660,
    audit_stream: Any = None,
) -> None:
    """Run the broker until interrupted. Blocks.

    Exactly one of socket_path (unix socket) or listen ("host:port" TCP) selects
    the transport.
    """
    if bool(socket_path) == bool(listen):
        raise ConfigurationError("exactly one of socket_path or listen must be given")
    stream = audit_stream if audit_stream is not None else sys.stdout
    policy = load_policy(Path(policy_path))
    server: _BrokerServer | _TCPBrokerServer
    if listen:
        endpoint = listen
        server = _TCPBrokerServer(
            split_listen(listen),
            _Handler,
            auth=auth,
            policy=policy,
            audit_stream=stream,
        )
    else:
        assert socket_path is not None
        endpoint = socket_path
        server = _BrokerServer(
            socket_path,
            _Handler,
            auth=auth,
            policy=policy,
            socket_mode=socket_mode,
            audit_stream=stream,
        )
    _audit(
        stream,
        event="startup",
        endpoint=endpoint,
        transport="tcp" if listen else "uds",
        policy=policy_path,
        allowed_repos=list(policy.allowed_repos),
        max_permissions=dict(policy.max_permissions),
        app_slug=auth.config.app_slug,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if socket_path:
            Path(socket_path).unlink(missing_ok=True)
        _audit(stream, event="shutdown", endpoint=endpoint)


def make_server_for_tests(
    socket_path: str,
    *,
    auth: GitHubAppAuth,
    policy: Policy,
    audit_stream: Any,
) -> _BrokerServer:
    """Build a broker server without blocking (tests drive serve_forever)."""
    return _BrokerServer(
        socket_path,
        _Handler,
        auth=auth,
        policy=policy,
        socket_mode=0o660,
        audit_stream=audit_stream,
    )


def make_tcp_server_for_tests(
    listen_address: tuple[str, int],
    *,
    auth: GitHubAppAuth,
    policy: Policy,
    audit_stream: Any,
) -> _TCPBrokerServer:
    """Build a TCP broker server without blocking (tests drive serve_forever)."""
    return _TCPBrokerServer(
        listen_address,
        _Handler,
        auth=auth,
        policy=policy,
        audit_stream=audit_stream,
    )
