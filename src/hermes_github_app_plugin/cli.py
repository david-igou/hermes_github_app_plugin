"""CLI commands, gh/git wrappers, and the git credential helper."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

import httpx

from .api import GitHubApi, repo_from_api_path
from .auth import GitHubAppAuth, auth_metadata, requires_app_jwt, split_repo
from .backends import (
    BROKER_SOCKET_ENV,
    BROKER_URL_ENV,
    BrokerDeniedError,
    LocalBackend,
    get_backend,
)
from .broker import serve as broker_serve
from .config import ConfigurationError, load_config, write_github_app_config

_CREDENTIAL_HELPER_PERMISSIONS = {"contents": "write"}


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Register `ghapp ...` / `hermes-github-app ...` subcommands."""
    subparsers = parser.add_subparsers(dest="github_app_command", required=True)

    setup = subparsers.add_parser("setup", help="Configure GitHub App auth")
    setup.add_argument("--repo", help="Optional OWNER/REPO to verify after setup")
    setup.add_argument(
        "--non-interactive", action="store_true", help="Read values from flags/env only"
    )
    setup.add_argument("--client-id", help="GitHub App client ID")
    setup.add_argument(
        "--installations",
        help="Installations as owner=id[,owner=id], e.g. david-igou=1,igou-io=2",
    )
    setup.add_argument("--private-key-path", help="Path to GitHub App private key PEM")
    setup.add_argument(
        "--private-key-cmd",
        help="Command printing the PEM, e.g. 'op read \"op://vault/item/private_key\"'",
    )
    setup.add_argument("--app-slug", help="Optional GitHub App slug, e.g. igou-dev")
    setup.add_argument(
        "--skip-verify", action="store_true", help="Write config without minting a token"
    )

    doctor = subparsers.add_parser("doctor", help="Run installation and auth diagnostics")
    doctor.add_argument("--repo", help="Optional OWNER/REPO access probe")
    doctor.add_argument("--skip-network", action="store_true", help="Skip GitHub network checks")

    status = subparsers.add_parser("status", help="Verify configuration and identity")
    status.add_argument("--repo", help="Optional OWNER/REPO access probe")

    token = subparsers.add_parser("token", help="Print a scoped installation token")
    token.add_argument("--repo", help="OWNER/REPO the token is scoped to")
    token.add_argument(
        "--permission",
        action="append",
        default=[],
        metavar="NAME=LEVEL",
        help="Permission to request (repeatable), e.g. contents=write. "
        "Default: the configured default permission set.",
    )
    token.add_argument("--json", action="store_true", help="Print JSON metadata and token")

    api = subparsers.add_parser("api", help="Call a GitHub REST API path")
    api.add_argument("path", help="GitHub REST API path, e.g. /repos/OWNER/REPO")
    api.add_argument("--method", default="GET")
    api.add_argument(
        "--repo", help="OWNER/REPO to scope the token to (inferred from /repos/ paths)"
    )
    api.add_argument("--data", help="JSON request body")

    serve = subparsers.add_parser("serve", help="Run the token broker (trusted side only)")
    serve.add_argument("--socket", help="Unix socket path to listen on")
    serve.add_argument(
        "--listen",
        help="TCP HOST:PORT to listen on instead of a unix socket (containerized "
        "mode — scope reachability with NetworkPolicy, the listener itself is "
        "unauthenticated)",
    )
    serve.add_argument("--policy", required=True, help="Path to policy.yaml")
    serve.add_argument(
        "--socket-mode", default="0660", help="Octal socket file mode (default 0660)"
    )


def main(args: argparse.Namespace | None = None) -> int:
    """Run the plugin CLI."""
    if args is None:
        parser = argparse.ArgumentParser(prog="ghapp")
        register_cli(parser)
        args = parser.parse_args()

    try:
        handler = _COMMANDS.get(args.github_app_command)
        if handler is None:
            raise ConfigurationError(f"unknown command: {args.github_app_command}")
        return int(handler(args))
    except (
        ConfigurationError,
        BrokerDeniedError,
        httpx.HTTPError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_permissions(pairs: list[str]) -> dict[str, str] | None:
    if not pairs:
        return None
    permissions: dict[str, str] = {}
    for pair in pairs:
        name, sep, level = pair.partition("=")
        if not sep or not name or not level:
            raise ConfigurationError(f"invalid --permission {pair!r}; expected NAME=LEVEL")
        permissions[name.strip()] = level.strip()
    return permissions


def gh_app_main() -> NoReturn:
    """Entry point for the `gh-app` wrapper."""
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print("usage: gh-app --repo OWNER/REPO [--permission NAME=LEVEL ...] [--] <gh args...>")
        print("Runs gh with GH_TOKEN/GITHUB_TOKEN set to a repo-scoped GitHub App token.")
        raise SystemExit(0)
    repo, permissions, child_args = _extract_wrapper_args(args)
    if not repo:
        print(
            "error: gh-app requires --repo OWNER/REPO (tokens are minted per repository)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    token = _mint_or_die(repo, permissions)
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    raise SystemExit(subprocess.call(["gh", *child_args], env=env))


def git_app_main() -> NoReturn:
    """Entry point for the `git-app` wrapper with temporary askpass credentials."""
    if len(sys.argv) <= 1 or sys.argv[1] in {"-h", "--help"}:
        print("usage: git-app --repo OWNER/REPO [--] <git args...>")
        print("Runs git with a temporary askpass helper backed by a repo-scoped App token.")
        raise SystemExit(0)
    repo, permissions, child_args = _extract_wrapper_args(sys.argv[1:])
    if not repo:
        print(
            "error: git-app requires --repo OWNER/REPO (tokens are minted per repository)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    token = _mint_or_die(repo, permissions or dict(_CREDENTIAL_HELPER_PERMISSIONS))
    with tempfile.TemporaryDirectory(prefix="git-app-") as temp_dir:
        askpass = Path(temp_dir) / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "*Username*) printf '%s\\n' 'x-access-token' ;;\n"
            f"*Password*) printf '%s\\n' '{token}' ;;\n"
            "*) printf '\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = os.environ.copy()
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        raise SystemExit(subprocess.call(["git", *child_args], env=env))


def git_credential_main() -> int:
    """Entry point for `git-credential-ghapp` (git credential helper protocol).

    gitconfig:
        [credential "https://github.com"]
            helper = ghapp
            useHttpPath = true
    """
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action != "get":
        # store/erase are valid no-ops: tokens are never persisted.
        return 0
    attributes: dict[str, str] = {}
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            attributes[key] = value

    if attributes.get("protocol") != "https":
        return 0
    host = attributes.get("host", "")
    if host not in {"github.com", "www.github.com"}:
        return 0
    path = attributes.get("path", "")
    repo = _repo_from_path(path)
    if not repo:
        print(
            "git-credential-ghapp: no repository path; "
            "set credential.useHttpPath=true for https://github.com",
            file=sys.stderr,
        )
        return 1
    try:
        token = get_backend().mint(repo, dict(_CREDENTIAL_HELPER_PERMISSIONS))
    except (ConfigurationError, BrokerDeniedError, httpx.HTTPError) as exc:
        print(f"git-credential-ghapp: {exc}", file=sys.stderr)
        return 1
    print("username=x-access-token")
    print(f"password={token.token}")
    return 0


_CREDENTIAL_PATH_PARTS = 2  # OWNER/REPO[.git]


def _repo_from_path(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) < _CREDENTIAL_PATH_PARTS:
        return None
    owner, name = parts[0], parts[1]
    name = name.removesuffix(".git")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def _mint_or_die(repo: str, permissions: dict[str, str] | None) -> str:
    try:
        return get_backend().mint(repo, permissions).token
    except (ConfigurationError, BrokerDeniedError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _setup(args: argparse.Namespace) -> int:
    """Interactively write GitHub App configuration."""
    print("GitHub App setup")
    print("Required values are unmarked. Optional prompts include '(optional)'.")
    installations_raw = _value_or_prompt(
        args.installations,
        "Installations (owner=id[,owner=id])",
        env="GITHUB_APP_INSTALLATIONS",
        required=True,
        non_interactive=bool(args.non_interactive),
    )
    installations: dict[str, str] = {}
    for pair in installations_raw.split(","):
        owner, sep, install_id = pair.strip().partition("=")
        if not sep or not owner or not install_id:
            raise ConfigurationError(f"invalid installations entry {pair!r}")
        installations[owner.strip().lower()] = install_id.strip()

    key_path = (args.private_key_path or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")).strip()
    key_cmd = (args.private_key_cmd or os.environ.get("GITHUB_APP_PRIVATE_KEY_CMD", "")).strip()
    if not key_path and not key_cmd and not args.non_interactive:
        key_path = input("GitHub App private key path (empty to use a command): ").strip()
        if not key_path:
            key_cmd = input("GitHub App private key command: ").strip()
    if not key_path and not key_cmd:
        raise ConfigurationError("provide --private-key-path or --private-key-cmd")

    values = {
        "client_id": _value_or_prompt(
            args.client_id,
            "GitHub App client ID",
            env="GITHUB_APP_CLIENT_ID",
            required=True,
            non_interactive=bool(args.non_interactive),
        ),
        "installations": installations,
        "private_key_path": key_path,
        "private_key_cmd": key_cmd,
        "app_slug": _value_or_prompt(
            args.app_slug,
            "GitHub App slug (optional)",
            env="GITHUB_APP_SLUG",
            required=False,
            non_interactive=bool(args.non_interactive),
        ),
    }
    if key_path:
        resolved = Path(key_path).expanduser()
        if not resolved.exists():
            raise ConfigurationError(f"private key file does not exist: {resolved}")
        _warn_private_key_permissions(resolved)
    written = write_github_app_config(values)
    print(f"Wrote GitHub App config to {written}")
    # Drop the legacy single-installation key when writing the new shape.
    write_github_app_config({"installation_id": ""})
    if args.skip_verify:
        print("Skipped verification. Run `ghapp doctor --repo OWNER/REPO` next.")
        return 0
    return _doctor(args.repo, skip_network=False)


def _broker_mode_checks(broker_socket: str, broker_url: str) -> list[tuple[str, bool, str]]:
    if broker_socket:
        return [
            ("broker mode (unix socket)", True, broker_socket),
            ("broker socket exists", Path(broker_socket).exists(), broker_socket),
        ]
    return [("broker mode (http)", True, broker_url)]


def _doctor(repo: str | None, *, skip_network: bool) -> int:
    """Run local and optional network diagnostics."""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("ghapp command installed", True, sys.argv[0]))
    for command in ("gh", "git", "gh-app", "git-app", "git-credential-ghapp"):
        found = shutil.which(command)
        checks.append((f"{command} on PATH", found is not None, found or "not found"))

    broker_socket = os.environ.get(BROKER_SOCKET_ENV, "")
    broker_url = os.environ.get(BROKER_URL_ENV, "")
    try:
        if broker_socket or broker_url:
            checks.extend(_broker_mode_checks(broker_socket, broker_url))
            if not skip_network:
                backend = get_backend()
                info = backend.describe()
                checks.append(("broker /status reachable", True, str(info.get("app_slug"))))
                if repo:
                    token = backend.mint(repo, {"contents": "read"})
                    checks.append(("scoped token minted via broker", True, token.redacted))
        else:
            config = load_config()
            owners = ", ".join(sorted(config.installations))
            checks.append(("GitHub App config loaded", True, f"installations: {owners}"))
            checks.append(("private key source", True, config.private_key_source))
            if config.key_source.kind == "path":
                key_file = Path(config.key_source.value).expanduser()
                checks.append(("private key file exists", key_file.exists(), str(key_file)))
                checks.append(
                    (
                        "private key file permissions",
                        _private_key_permissions_ok(key_file),
                        _mode(key_file),
                    )
                )
            if not skip_network:
                auth = GitHubAppAuth(config)
                app_result = auth.app_request("GET", "/app")["result"]
                checks.append(("/app API reachable", True, str(app_result.get("slug", "ok"))))
                if repo:
                    token = auth.mint_for_repo(repo, {"contents": "read"}, force_refresh=True)
                    checks.append(("scoped token minted", True, token.redacted))
                    api = GitHubApi(LocalBackend(auth), api_url=config.github_api_url)
                    probe = api.request(
                        "GET", f"/repos/{repo}", repo=repo, permissions={"contents": "read"}
                    )["result"]
                    checks.append(
                        ("repository access verified", True, str(probe.get("full_name", repo)))
                    )
    except Exception as exc:
        checks.append(("GitHub App auth/config", False, str(exc)))

    success = all(ok for _, ok, _ in checks)
    for name, ok, detail in checks:
        marker = "✓" if ok else "✗"
        print(f"{marker} {name}: {detail}")
    if success:
        print(
            "Doctor passed. GitHub App identity is ready."
            if not skip_network
            else "Local doctor passed."
        )
        return 0
    print("Doctor found issues. Fix the failed checks above and rerun.", file=sys.stderr)
    return 1


def _status(repo: str | None) -> int:
    backend = get_backend()
    info = backend.describe()
    repo_probe = None
    if repo:
        api = GitHubApi(backend, api_url=str(info.get("github_api_url", "https://api.github.com")))
        repo_probe = api.request(
            "GET", f"/repos/{repo}", repo=repo, permissions={"contents": "read"}
        )["result"]
    print(
        json.dumps(
            {"success": True, "backend": info, "repository_probe": repo_probe},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _token(repo: str | None, *, permissions: dict[str, str] | None, json_output: bool) -> int:
    if not repo:
        raise ConfigurationError(
            "token requires --repo OWNER/REPO; tokens are always minted per repository"
        )
    token = get_backend().mint(repo, permissions)
    if json_output:
        print(json.dumps({"token": token.token, "auth": auth_metadata(token, repo=repo)}, indent=2))
    else:
        print(token.token)
    return 0


def _api(method: str, path: str, *, repo: str | None, body: dict[str, Any] | None) -> int:
    if requires_app_jwt(path):
        if os.environ.get(BROKER_SOCKET_ENV, "") or os.environ.get(BROKER_URL_ENV, ""):
            raise ConfigurationError(
                "GitHub App JWT endpoints (/app...) are not available in broker mode"
            )
        result = GitHubAppAuth(load_config()).app_request(method, path, json_body=body)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if not repo:
        repo = repo_from_api_path(path)
    if not repo:
        raise ConfigurationError("api requires --repo OWNER/REPO to scope the token")
    backend = get_backend()
    info = backend.describe()
    api = GitHubApi(backend, api_url=str(info.get("github_api_url", "https://api.github.com")))
    result = api.request(method, path, repo=repo, json_body=body)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _serve(socket_path: str | None, policy_path: str, socket_mode: str, listen: str | None) -> int:
    try:
        mode = int(socket_mode, 8)
    except ValueError as exc:
        raise ConfigurationError(f"invalid --socket-mode {socket_mode!r}") from exc
    if bool(socket_path) == bool(listen):
        raise ConfigurationError("exactly one of --socket or --listen is required")
    for env_name in (BROKER_SOCKET_ENV, BROKER_URL_ENV):
        if os.environ.get(env_name, ""):
            raise ConfigurationError(
                f"refusing to serve with {env_name} set — the broker must "
                "mint locally, not recurse into another broker"
            )
    broker_serve(
        socket_path=socket_path or None,
        listen=listen or None,
        policy_path=policy_path,
        auth=GitHubAppAuth(load_config()),
        socket_mode=mode,
    )
    return 0


def _value_or_prompt(
    value: str | None,
    label: str,
    *,
    env: str,
    required: bool,
    non_interactive: bool,
) -> str:
    """Return a provided/env value or prompt for it."""
    resolved = value or os.environ.get(env, "")
    if resolved:
        return resolved.strip()
    if non_interactive:
        if required:
            raise ConfigurationError(f"missing required value: {label} (or {env})")
        return ""
    resolved = input(f"{label}: ").strip()
    if required and not resolved:
        raise ConfigurationError(f"missing required value: {label}")
    return resolved


def _private_key_permissions_ok(path: Path) -> bool:
    if not path.exists():
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0


def _warn_private_key_permissions(path: Path) -> None:
    if not _private_key_permissions_ok(path):
        print(
            f"warning: {path} is readable by group/other ({_mode(path)}). "
            "Run `chmod 600 <key>` to lock it down.",
            file=sys.stderr,
        )


def _mode(path: Path) -> str:
    if not path.exists():
        return "missing"
    return oct(stat.S_IMODE(path.stat().st_mode))


def _extract_wrapper_args(args: list[str]) -> tuple[str | None, dict[str, str] | None, list[str]]:
    """Parse --repo/--permission out of wrapper argv, returning the child argv."""
    repo: str | None = None
    permission_pairs: list[str] = []
    child_args: list[str] = []
    iterator = iter(args)
    for arg in iterator:
        if arg == "--repo":
            repo = next(iterator, None)
            if repo is None:
                raise ConfigurationError("--repo requires OWNER/REPO")
        elif arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg == "--permission":
            pair = next(iterator, None)
            if pair is None:
                raise ConfigurationError("--permission requires NAME=LEVEL")
            permission_pairs.append(pair)
        elif arg.startswith("--permission="):
            permission_pairs.append(arg.split("=", 1)[1])
        elif arg == "--":
            child_args.extend(iterator)
            break
        else:
            child_args.append(arg)
    if not child_args:
        raise ConfigurationError("missing command to run")
    if repo:
        split_repo(repo)
    return repo, _parse_permissions(permission_pairs), child_args


_COMMANDS: dict[str, Any] = {
    "setup": _setup,
    "doctor": lambda a: _doctor(a.repo, skip_network=bool(a.skip_network)),
    "status": lambda a: _status(a.repo),
    "token": lambda a: _token(
        a.repo, permissions=_parse_permissions(a.permission), json_output=bool(a.json)
    ),
    "api": lambda a: _api(
        a.method, a.path, repo=a.repo, body=json.loads(a.data) if a.data else None
    ),
    "serve": lambda a: _serve(a.socket, a.policy, a.socket_mode, a.listen),
}


if __name__ == "__main__":
    raise SystemExit(main())
