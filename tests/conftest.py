"""Shared test fixtures and fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from hermes_github_app_plugin.auth import InstallationToken
from hermes_github_app_plugin.config import GitHubAppConfig, KeySource

PRIVATE_KEY = "[REDACTED PRIVATE KEY]\n"


def make_config(**overrides: Any) -> GitHubAppConfig:
    values: dict[str, Any] = {
        "client_id": "Iv1.testclientid",
        "installations": {"exampleorg": "111", "exampleuser": "222"},
        "key_source": KeySource("inline", PRIVATE_KEY),
        "app_slug": "test-agent",
    }
    values.update(overrides)
    return GitHubAppConfig(**values)


def make_token(**overrides: Any) -> InstallationToken:
    values: dict[str, Any] = {
        "token": "ghs_faketoken0123456789",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "installation_id": "111",
        "client_id": "Iv1.testclientid",
        "app_slug": "test-agent",
        "owner": "exampleorg",
        "repositories": ("exampleorg/repo",),
        "permissions": {"contents": "write"},
    }
    values.update(overrides)
    return InstallationToken(**values)


class FakeBackend:
    """Duck-typed TokenBackend recording mint calls."""

    mode = "fake"

    def __init__(self, token: InstallationToken | None = None) -> None:
        self.token = token or make_token()
        self.mint_calls: list[tuple[str, dict[str, str] | None]] = []

    def mint(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> InstallationToken:
        self.mint_calls.append((repo, permissions))
        return self.token

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.mode,
            "app_slug": self.token.app_slug,
            "github_api_url": "https://api.github.com",
        }


@pytest.fixture(autouse=True)
def _clean_ghapp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient GitHub App configuration out of tests."""
    for var in (
        "GHAPP_BROKER_SOCKET",
        "GHAPP_CONFIG",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_INSTALLATIONS",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY_CMD",
        "GITHUB_APP_SLUG",
        "GITHUB_API_URL",
    ):
        monkeypatch.delenv(var, raising=False)
