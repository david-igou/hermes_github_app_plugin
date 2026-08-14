"""Tests for backend selection and the local backend."""

from __future__ import annotations

import pytest

from hermes_github_app_plugin.auth import GitHubAppAuth
from hermes_github_app_plugin.backends import BrokerBackend, LocalBackend, get_backend
from hermes_github_app_plugin.config import ConfigurationError
from tests.conftest import make_config


def test_get_backend_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHAPP_BROKER_SOCKET", raising=False)
    monkeypatch.delenv("GHAPP_BROKER_URL", raising=False)
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATIONS", "exampleorg=1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake PRIVATE KEY")

    backend = get_backend()

    assert isinstance(backend, LocalBackend)
    assert backend.mode == "local"


def test_get_backend_prefers_broker_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHAPP_BROKER_SOCKET", "/run/ghbroker/ghbroker.sock")

    backend = get_backend()

    assert isinstance(backend, BrokerBackend)
    assert backend.socket_path == "/run/ghbroker/ghbroker.sock"


def test_local_backend_describe_has_no_key_material() -> None:
    backend = LocalBackend(GitHubAppAuth(make_config()))

    info = backend.describe()

    assert info["backend"] == "local"
    assert info["installations"] == {"exampleorg": "111", "exampleuser": "222"}
    assert "BEGIN" not in str(info)
    assert info["private_key_source"] == "inline (GITHUB_APP_PRIVATE_KEY)"


def test_get_backend_uses_broker_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHAPP_BROKER_SOCKET", raising=False)
    monkeypatch.setenv("GHAPP_BROKER_URL", "http://ghbroker.hermes-k8s.svc:8085")

    backend = get_backend()

    assert isinstance(backend, BrokerBackend)
    assert backend.endpoint == "http://ghbroker.hermes-k8s.svc:8085"


def test_get_backend_socket_wins_over_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHAPP_BROKER_SOCKET", "/run/ghbroker/ghbroker.sock")
    monkeypatch.setenv("GHAPP_BROKER_URL", "http://ghbroker.hermes-k8s.svc:8085")

    backend = get_backend()

    assert isinstance(backend, BrokerBackend)
    assert backend.socket_path == "/run/ghbroker/ghbroker.sock"


def test_broker_backend_requires_exactly_one_endpoint() -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        BrokerBackend()
    with pytest.raises(ConfigurationError, match="exactly one"):
        BrokerBackend("/run/x.sock", url="http://x")
