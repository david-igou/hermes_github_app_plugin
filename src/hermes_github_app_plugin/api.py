"""GitHub REST/GraphQL calls on top of a token backend."""

from __future__ import annotations

from typing import Any

import httpx

from .auth import InstallationToken, auth_metadata
from .backends import BrokerBackend, LocalBackend
from .config import ConfigurationError

_API_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
}


class GitHubApi:
    """Call the GitHub API with per-repo tokens minted by a backend."""

    def __init__(
        self,
        backend: LocalBackend | BrokerBackend,
        client: httpx.Client | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._backend = backend
        self._client = client or httpx.Client(timeout=20)
        self._api_url = api_url.rstrip("/")

    @property
    def backend(self) -> LocalBackend | BrokerBackend:
        return self._backend

    def token_for(self, repo: str, permissions: dict[str, str] | None = None) -> InstallationToken:
        return self._backend.mint(repo, permissions or dict(_API_PERMISSIONS))

    def request(
        self,
        method: str,
        path: str,
        *,
        repo: str,
        permissions: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a REST path with a token scoped to `repo`."""
        token = self.token_for(repo, permissions)
        url = path if path.startswith("http") else f"{self._api_url}/{path.lstrip('/')}"
        response = self._client.request(
            method.upper(),
            url,
            headers=_headers(token.token),
            json=json_body,
            params=params,
        )
        response.raise_for_status()
        parsed = response.json() if response.content else {"ok": True}
        return {
            "auth": auth_metadata(token, repo=repo),
            "status_code": response.status_code,
            "result": parsed,
        }

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        repo: str,
        permissions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call GraphQL with a token scoped to `repo`.

        GraphQL has no URL path to scope by, so the caller must say which
        repository the query concerns; the token is minted for that repo only.
        """
        if not repo:
            raise ConfigurationError("graphql requires repo (OWNER/REPO) to scope the token")
        token = self.token_for(repo, permissions)
        response = self._client.post(
            f"{self._api_url}/graphql",
            headers=_headers(token.token),
            json={"query": query, "variables": variables or {}},
        )
        response.raise_for_status()
        return {
            "auth": auth_metadata(token, repo=repo),
            "status_code": response.status_code,
            "result": response.json(),
        }


_REPO_PATH_PARTS = 3  # /repos/OWNER/REPO/...


def repo_from_api_path(path: str) -> str | None:
    """Infer OWNER/REPO from a /repos/... REST path, else None."""
    parts = [p for p in path.split("?", 1)[0].split("/") if p]
    if len(parts) >= _REPO_PATH_PARTS and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return None


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
