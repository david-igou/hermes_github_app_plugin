"""GitHub App JWT and installation-token handling."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from .config import ConfigurationError, GitHubAppConfig

_MIN_REDACT_LENGTH = 8

#: Refresh a cached token when it is this close to expiry.
_EXPIRY_SLACK = timedelta(minutes=5)


def split_repo(repo: str) -> tuple[str, str]:
    """Split OWNER/REPO, validating the shape."""
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ConfigurationError(f"expected OWNER/REPO, got {repo!r}")
    return owner, name


@dataclass(frozen=True)
class InstallationToken:
    """GitHub App installation token plus metadata."""

    token: str
    expires_at: datetime
    installation_id: str
    client_id: str
    app_slug: str | None
    owner: str | None = None
    repositories: tuple[str, ...] = ()
    permissions: dict[str, str] = field(default_factory=dict)

    @property
    def redacted(self) -> str:
        """Return a safe representation for logs/tool output."""
        if len(self.token) <= _MIN_REDACT_LENGTH:
            return "***"
        return f"{self.token[:4]}…{self.token[-4:]}"


def _scope_key(
    installation_id: str,
    repositories: tuple[str, ...],
    permissions: dict[str, str] | None,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]:
    return (
        installation_id,
        tuple(sorted(repositories)),
        tuple(sorted((permissions or {}).items())),
    )


class GitHubAppAuth:
    """Mint and cache short-lived, down-scoped installation access tokens."""

    def __init__(self, config: GitHubAppConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=20)
        self._cache: dict[Any, InstallationToken] = {}

    @property
    def config(self) -> GitHubAppConfig:
        return self._config

    def create_jwt(self) -> str:
        """Create a GitHub App JWT for installation-token exchange."""
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._config.client_id}
        encoded = jwt.encode(payload, self._config.resolve_private_key(), algorithm="RS256")
        return str(encoded)

    def get_installation_token(
        self,
        *,
        owner: str | None = None,
        repositories: tuple[str, ...] | list[str] = (),
        permissions: dict[str, str] | None = None,
        force_refresh: bool = False,
    ) -> InstallationToken:
        """Return a valid installation token for the requested scope.

        `repositories` holds bare repo names (no owner); `owner` picks the
        installation. An empty scope mints a full-installation token — callers
        should prefer passing both a repository and a permission set.
        """
        owner_key, installation_id = self._config.installation_for(owner)
        repos = tuple(repositories)
        cache_key = _scope_key(installation_id, repos, permissions)
        cached = self._cache.get(cache_key)
        if (
            not force_refresh
            and cached is not None
            and cached.expires_at > datetime.now(timezone.utc) + _EXPIRY_SLACK
        ):
            return cached

        body: dict[str, Any] = {}
        if repos:
            body["repositories"] = list(repos)
        if permissions:
            body["permissions"] = dict(permissions)
        response = self._client.post(
            f"{self._config.github_api_url}/app/installations/{installation_id}/access_tokens",
            headers=self._headers(f"Bearer {self.create_jwt()}"),
            json=body or None,
        )
        response.raise_for_status()
        data = response.json()
        expires_at_raw = str(data["expires_at"])
        granted = data.get("permissions")
        token = InstallationToken(
            token=str(data["token"]),
            expires_at=datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00")),
            installation_id=installation_id,
            client_id=self._config.client_id,
            app_slug=self._config.app_slug,
            owner=owner_key,
            repositories=tuple(
                r["full_name"] for r in data.get("repositories", []) if isinstance(r, dict)
            )
            or repos,
            permissions=dict(granted) if isinstance(granted, dict) else dict(permissions or {}),
        )
        self._cache[cache_key] = token
        return token

    def mint_for_repo(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken:
        """Mint a single-repository token; permissions default from config."""
        owner, name = split_repo(repo)
        return self.get_installation_token(
            owner=owner,
            repositories=(name,),
            permissions=permissions or dict(self._config.default_permissions),
            force_refresh=force_refresh,
        )

    def app_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a GitHub App endpoint using the app JWT.

        Endpoints like `GET /app` authenticate as the GitHub App itself and
        reject installation access tokens.
        """
        url = (
            path if path.startswith("http") else f"{self._config.github_api_url}/{path.lstrip('/')}"
        )
        response = self._client.request(
            method.upper(),
            url,
            headers=self._headers(f"Bearer {self.create_jwt()}"),
            json=json_body,
            params=params,
        )
        response.raise_for_status()
        return {
            "auth": {
                "auth_mode": "github_app_jwt",
                "client_id": self._config.client_id,
                "app_slug": self._config.app_slug,
            },
            "status_code": response.status_code,
            "result": response.json() if response.content else {"ok": True},
        }

    @staticmethod
    def _headers(authorization: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": authorization,
            "X-GitHub-Api-Version": "2022-11-28",
        }


def auth_metadata(token: InstallationToken, *, repo: str | None = None) -> dict[str, Any]:
    """Build safe auth metadata for tool responses."""
    actor = f"{token.app_slug}[bot]" if token.app_slug else None
    return {
        "auth_mode": "github_app",
        "client_id": token.client_id,
        "app_slug": token.app_slug,
        "installation_id": token.installation_id,
        "actor_expected": actor,
        "repository": repo,
        "scoped_repositories": list(token.repositories),
        "scoped_permissions": dict(token.permissions),
        "token": token.redacted,
        "expires_at": token.expires_at.isoformat(),
    }


def requires_app_jwt(path: str) -> bool:
    """Return True for GitHub App endpoints that require app JWT auth."""
    path_only = path.split("?", 1)[0]
    if path_only.startswith("http"):
        try:
            path_only = urlparse(path_only).path
        except ValueError:
            return False
    normalized = "/" + path_only.lstrip("/")
    return normalized == "/app" or normalized.startswith("/app/")
