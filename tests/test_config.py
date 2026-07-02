"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_github_app_plugin.config import (
    ANY_OWNER,
    ConfigurationError,
    KeySource,
    load_config,
)
from tests.conftest import PRIVATE_KEY, make_config


def test_load_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATIONS", "ExampleOrg=456, exampleuser=789")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", PRIVATE_KEY)

    config = load_config()

    assert config.client_id == "123"
    assert config.installations == {"exampleorg": "456", "exampleuser": "789"}
    assert config.resolve_private_key() == PRIVATE_KEY


def test_load_config_legacy_installation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", PRIVATE_KEY)

    config = load_config()

    assert config.installations == {ANY_OWNER: "456"}
    assert config.installation_for("anything") == ("anything", "456")


def test_load_config_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(PRIVATE_KEY, encoding="utf-8")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"""
github_app:
  client_id: 111
  installations:
    ExampleOrg: 333
    exampleuser: 444
  private_key_path: {key_path}
  app_slug: test-agent
  default_permissions:
    contents: write
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    config = load_config()

    assert config.client_id == "111"
    assert config.installations == {"exampleorg": "333", "exampleuser": "444"}
    assert config.app_slug == "test-agent"
    assert config.default_permissions == {"contents": "write"}


def test_ghapp_config_env_overrides_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "ghapp.yaml"
    config_file.write_text(
        """
github_app:
  client_id: 999
  installations:
    someorg: 888
  private_key: fake
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GHAPP_CONFIG", str(config_file))

    config = load_config()

    assert config.client_id == "999"
    assert config.installations == {"someorg": "888"}


def test_installation_for_owner_lookup() -> None:
    config = make_config()

    assert config.installation_for("ExampleOrg") == ("exampleorg", "111")
    with pytest.raises(ConfigurationError, match="no installation configured"):
        config.installation_for("stranger")
    with pytest.raises(ConfigurationError, match=r"an owner .* is required"):
        config.installation_for(None)


def test_installation_for_single_installation_default() -> None:
    config = make_config(installations={"exampleorg": "111"})

    assert config.installation_for(None) == ("exampleorg", "111")


def test_invalid_installations_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATIONS", "not-a-pair")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", PRIVATE_KEY)

    with pytest.raises(ConfigurationError, match="invalid GITHUB_APP_INSTALLATIONS"):
        load_config()


def test_key_source_cmd_resolves_at_call_time(tmp_path: Path) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(PRIVATE_KEY, encoding="utf-8")
    source = KeySource("cmd", f"cat {key_path}")

    assert source.resolve() == PRIVATE_KEY
    assert "PRIVATE KEY" not in source.display


def test_key_source_cmd_rejects_non_pem_output() -> None:
    source = KeySource("cmd", "echo not-a-key")

    with pytest.raises(ConfigurationError, match="does not look like a PEM key"):
        source.resolve()


def test_key_source_cmd_failure_raises() -> None:
    source = KeySource("cmd", "false")

    with pytest.raises(ConfigurationError, match="private_key_cmd failed"):
        source.resolve()
