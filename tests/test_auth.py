"""Tests for GitHub App auth: scoped minting, caching, metadata."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from hermes_github_app_plugin.auth import (
    GitHubAppAuth,
    InstallationToken,
    auth_metadata,
    requires_app_jwt,
    split_repo,
)
from hermes_github_app_plugin.config import ConfigurationError
from tests.conftest import make_config


def _mint_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content) if request.content else {}
        return httpx.Response(
            201,
            json={
                "token": f"ghs_minted{len(seen)}",
                "expires_at": "2030-01-01T00:00:00Z",
                "repositories": [
                    {"full_name": f"exampleorg/{name}"} for name in body.get("repositories", [])
                ],
                "permissions": body.get("permissions", {"contents": "write"}),
            },
        )

    return httpx.MockTransport(handler)


def _auth(seen: list[httpx.Request]) -> GitHubAppAuth:
    return GitHubAppAuth(make_config(), client=httpx.Client(transport=_mint_transport(seen)))


def test_mint_sends_scoped_body_and_selects_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jwt.encode", lambda *_, **__: "jwt-token")
    seen: list[httpx.Request] = []
    auth = _auth(seen)

    token = auth.mint_for_repo("ExampleOrg/example-repo", {"contents": "write"})

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/app/installations/111/access_tokens"
    assert request.headers["Authorization"] == "Bearer jwt-token"
    body = json.loads(request.content)
    assert body == {"repositories": ["example-repo"], "permissions": {"contents": "write"}}
    assert token.repositories == ("exampleorg/example-repo",)
    assert token.permissions == {"contents": "write"}
    assert token.owner == "exampleorg"


def test_mint_uses_default_permissions_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jwt.encode", lambda *_, **__: "jwt-token")
    seen: list[httpx.Request] = []
    auth = GitHubAppAuth(
        make_config(default_permissions={"contents": "read"}),
        client=httpx.Client(transport=_mint_transport(seen)),
    )

    auth.mint_for_repo("exampleuser/repo")

    body = json.loads(seen[0].content)
    assert body["permissions"] == {"contents": "read"}
    assert seen[0].url.path == "/app/installations/222/access_tokens"


def test_mint_caches_per_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jwt.encode", lambda *_, **__: "jwt-token")
    seen: list[httpx.Request] = []
    auth = _auth(seen)

    first = auth.mint_for_repo("exampleorg/repo", {"contents": "write"})
    again = auth.mint_for_repo("exampleorg/repo", {"contents": "write"})
    other_scope = auth.mint_for_repo("exampleorg/repo", {"contents": "read"})

    assert first.token == again.token
    assert other_scope.token != first.token
    assert len(seen) == 2


def test_mint_unknown_owner_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jwt.encode", lambda *_, **__: "jwt-token")
    auth = _auth([])

    with pytest.raises(ConfigurationError, match="no installation configured"):
        auth.mint_for_repo("stranger/repo")


def test_split_repo_validates_shape() -> None:
    assert split_repo("owner/name") == ("owner", "name")
    for bad in ("ownername", "owner/", "/name", "owner/name/extra"):
        with pytest.raises(ConfigurationError, match="expected OWNER/REPO"):
            split_repo(bad)


def test_auth_metadata_redacts_token() -> None:
    token = InstallationToken(
        token="ghu_ab...wxyz",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        installation_id="456",
        client_id="123",
        app_slug="test-agent",
        repositories=("exampleorg/example-repo",),
        permissions={"contents": "write"},
    )

    metadata = auth_metadata(token, repo="ExampleOrg/example-repo")

    assert metadata["auth_mode"] == "github_app"
    assert metadata["actor_expected"] == "test-agent[bot]"
    assert metadata["token"] == "ghu_…wxyz"
    assert metadata["scoped_repositories"] == ["exampleorg/example-repo"]
    assert metadata["scoped_permissions"] == {"contents": "write"}


def test_requires_app_jwt() -> None:
    assert requires_app_jwt("/app")
    assert requires_app_jwt("/app/installations")
    assert requires_app_jwt("https://api.github.com/app")
    assert not requires_app_jwt("/repos/owner/repo")
    assert not requires_app_jwt("/apps/slug")
