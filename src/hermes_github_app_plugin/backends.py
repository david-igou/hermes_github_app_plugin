"""Token backends: local in-process minting vs. the host-side broker socket.

The client surface (credential helper, gh-app, CLI, Hermes tools) never talks
to GitHub's app-auth endpoints directly — it asks a backend for a token. In
local mode the backend holds the App private key and mints in-process (the
devcontainer case). In broker mode the key lives with a separate daemon behind
a unix socket and this process holds no key material at all (the Hermes
terminal-container case).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Protocol

import httpx

from .auth import GitHubAppAuth, InstallationToken, split_repo
from .config import ConfigurationError, load_config

BROKER_SOCKET_ENV = "GHAPP_BROKER_SOCKET"

_BROKER_BASE_URL = "http://ghapp-broker"


class TokenBackend(Protocol):
    """Common backend interface."""

    @property
    def mode(self) -> str: ...

    def mint(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken: ...

    def describe(self) -> dict[str, Any]: ...


class LocalBackend:
    """Mint tokens in-process using the configured App private key."""

    mode = "local"

    def __init__(self, auth: GitHubAppAuth | None = None) -> None:
        self._auth = auth or GitHubAppAuth(load_config())

    @property
    def auth(self) -> GitHubAppAuth:
        return self._auth

    def mint(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken:
        return self._auth.mint_for_repo(repo, permissions, force_refresh=force_refresh)

    def describe(self) -> dict[str, Any]:
        config = self._auth.config
        return {
            "backend": self.mode,
            "client_id": config.client_id,
            "app_slug": config.app_slug,
            "installations": dict(config.installations),
            "private_key_source": config.private_key_source,
            "github_api_url": config.github_api_url,
            "default_permissions": dict(config.default_permissions),
        }


class BrokerBackend:
    """Request tokens from the ghapp broker over its unix socket."""

    mode = "broker"

    def __init__(self, socket_path: str, client: httpx.Client | None = None) -> None:
        self._socket_path = socket_path
        self._client = client or httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url=_BROKER_BASE_URL,
            timeout=30,
        )

    @property
    def socket_path(self) -> str:
        return self._socket_path

    def mint(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken:
        split_repo(repo)  # validate shape before it hits the wire
        body: dict[str, Any] = {"repo": repo}
        if permissions:
            body["permissions"] = dict(permissions)
        if force_refresh:
            body["force_refresh"] = True
        try:
            response = self._client.post("/token", json=body)
        except httpx.TransportError as exc:
            raise ConfigurationError(
                f"cannot reach the ghapp broker at {self._socket_path}: {exc}"
            ) from exc
        data = response.json()
        if response.status_code != httpx.codes.OK:
            raise BrokerDeniedError(
                str(data.get("error", f"broker returned {response.status_code}")),
                status_code=response.status_code,
            )
        expires_raw = str(data["expires_at"])
        return InstallationToken(
            token=str(data["token"]),
            expires_at=datetime.fromisoformat(expires_raw.replace("Z", "+00:00")),
            installation_id=str(data.get("installation_id", "")),
            client_id=str(data.get("client_id", "")),
            app_slug=data.get("app_slug"),
            owner=data.get("owner"),
            repositories=tuple(data.get("repositories", ())),
            permissions=dict(data.get("permissions", {})),
        )

    def describe(self) -> dict[str, Any]:
        try:
            response = self._client.get("/status")
        except httpx.TransportError as exc:
            raise ConfigurationError(
                f"cannot reach the ghapp broker at {self._socket_path}: {exc}"
            ) from exc
        response.raise_for_status()
        info = dict(response.json())
        info.update({"backend": self.mode, "broker_socket": self._socket_path})
        return info


class BrokerDeniedError(RuntimeError):
    """The broker refused to mint (policy denial or upstream error)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_backend() -> LocalBackend | BrokerBackend:
    """Select the backend: broker when GHAPP_BROKER_SOCKET is set, else local."""
    socket_path = os.environ.get(BROKER_SOCKET_ENV, "")
    if socket_path:
        return BrokerBackend(socket_path)
    return LocalBackend()
