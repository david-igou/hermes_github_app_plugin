"""Tests for the broker: policy evaluation and the unix-socket server."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermes_github_app_plugin.backends import BrokerBackend, BrokerDeniedError
from hermes_github_app_plugin.broker import (
    Policy,
    PolicyError,
    load_policy,
    make_server_for_tests,
    make_tcp_server_for_tests,
    serve,
    split_listen,
)
from hermes_github_app_plugin.config import ConfigurationError
from tests.conftest import make_config, make_token

POLICY = Policy(
    allowed_repos=("exampleorg/repo", "exampleorg/prefix-*"),
    max_permissions={"contents": "write", "issues": "read"},
    default_permissions={"contents": "read"},
)


def test_policy_denies_unlisted_repo() -> None:
    allowed, reason, _ = POLICY.evaluate("exampleorg/other", {"contents": "read"})

    assert not allowed
    assert "not in the broker policy" in reason


def test_policy_glob_match() -> None:
    allowed, _, granted = POLICY.evaluate("exampleorg/prefix-thing", {"contents": "write"})

    assert allowed
    assert granted == {"contents": "write"}


def test_policy_denies_unknown_permission() -> None:
    allowed, reason, _ = POLICY.evaluate("exampleorg/repo", {"secrets": "write"})

    assert not allowed
    assert "'secrets' is not allowed" in reason


def test_policy_denies_level_above_max() -> None:
    allowed, reason, _ = POLICY.evaluate("exampleorg/repo", {"issues": "write"})

    assert not allowed
    assert "exceeds the policy maximum" in reason


def test_policy_defaults_when_no_permissions_requested() -> None:
    allowed, _, granted = POLICY.evaluate("exampleorg/repo", None)

    assert allowed
    assert granted == {"contents": "read"}


def test_load_policy_validates(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"

    with pytest.raises(PolicyError, match="does not exist"):
        load_policy(path)

    path.write_text("policy: {allowed_repos: []}\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="allowed_repos"):
        load_policy(path)

    path.write_text(
        """
policy:
  allowed_repos:
    - ExampleOrg/Repo
  max_permissions:
    contents: write
""",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert policy.allowed_repos == ("exampleorg/repo",)
    assert policy.default_permissions == {"contents": "read"}


def test_load_policy_rejects_defaults_above_max(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
policy:
  allowed_repos:
    - exampleorg/repo
  max_permissions:
    contents: read
  default_permissions:
    contents: write
""",
        encoding="utf-8",
    )

    with pytest.raises(PolicyError, match="exceed max_permissions"):
        load_policy(path)


class _FakeAuth:
    """Duck-typed GitHubAppAuth for the broker server."""

    def __init__(self) -> None:
        self.config = make_config()
        self.mint_calls: list[tuple[str, dict[str, str] | None]] = []
        self.fail_with: Exception | None = None

    def mint_for_repo(
        self,
        repo: str,
        permissions: dict[str, str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> object:
        if self.fail_with is not None:
            raise self.fail_with
        self.mint_calls.append((repo, permissions))
        return make_token(repositories=(repo.lower(),), permissions=dict(permissions or {}))


@pytest.fixture()
def broker(tmp_path: Path) -> Iterator[tuple[BrokerBackend, _FakeAuth, io.StringIO]]:
    socket_path = str(tmp_path / "broker.sock")
    auth = _FakeAuth()
    audit = io.StringIO()
    server = make_server_for_tests(
        socket_path,
        auth=auth,  # type: ignore[arg-type]
        policy=POLICY,
        audit_stream=audit,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield BrokerBackend(socket_path), auth, audit
    finally:
        server.shutdown()
        server.server_close()


def test_broker_grants_within_policy(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, audit = broker

    token = backend.mint("ExampleOrg/repo", {"contents": "write"})

    assert token.token.startswith("ghs_")
    assert auth.mint_calls == [("ExampleOrg/repo", {"contents": "write"})]
    lines = [json.loads(line) for line in audit.getvalue().splitlines()]
    granted = [line for line in lines if line.get("decision") == "granted"]
    assert granted and granted[0]["repo"] == "ExampleOrg/repo"
    assert "peer_uid" in granted[0]


def test_broker_applies_default_permissions(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, _ = broker

    backend.mint("exampleorg/repo")

    assert auth.mint_calls == [("exampleorg/repo", {"contents": "read"})]


def test_broker_denies_out_of_policy(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, audit = broker

    with pytest.raises(BrokerDeniedError, match="not in the broker policy") as excinfo:
        backend.mint("exampleorg/forbidden", {"contents": "read"})

    assert excinfo.value.status_code == 403
    assert auth.mint_calls == []
    lines = [json.loads(line) for line in audit.getvalue().splitlines()]
    assert any(line.get("decision") == "denied" for line in lines)


def test_broker_rejects_bad_request(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, _, _ = broker

    with pytest.raises(ConfigurationError, match="expected OWNER/REPO"):
        backend.mint("not-a-repo")


def test_broker_maps_upstream_failure_to_502(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, _ = broker
    auth.fail_with = ConfigurationError("key exploded")

    with pytest.raises(BrokerDeniedError, match="mint failed") as excinfo:
        backend.mint("exampleorg/repo", {"contents": "read"})

    assert excinfo.value.status_code == 502


def test_broker_status_exposes_policy_not_key(
    broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, _, _ = broker

    info = backend.describe()

    assert info["app_slug"] == "test-agent"
    assert info["backend"] == "broker"
    assert info["policy"]["max_permissions"] == {"contents": "write", "issues": "read"}
    assert "private_key" not in json.dumps(info)


@pytest.fixture()
def tcp_broker() -> Iterator[tuple[BrokerBackend, _FakeAuth, io.StringIO]]:
    auth = _FakeAuth()
    audit = io.StringIO()
    server = make_tcp_server_for_tests(
        ("127.0.0.1", 0),  # ephemeral port
        auth=auth,  # type: ignore[arg-type]
        policy=POLICY,
        audit_stream=audit,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield BrokerBackend(url=f"http://127.0.0.1:{port}"), auth, audit
    finally:
        server.shutdown()
        server.server_close()


def test_tcp_broker_grants_within_policy(
    tcp_broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, audit = tcp_broker

    token = backend.mint("ExampleOrg/repo", {"contents": "write"})

    assert token.token.startswith("ghs_")
    assert auth.mint_calls == [("ExampleOrg/repo", {"contents": "write"})]
    lines = [json.loads(line) for line in audit.getvalue().splitlines()]
    granted = [line for line in lines if line.get("decision") == "granted"]
    assert granted and granted[0]["repo"] == "ExampleOrg/repo"
    # TCP has no SO_PEERCRED; the peer is audited by address instead.
    assert granted[0]["peer_addr"] == "127.0.0.1"
    assert "peer_uid" not in granted[0]


def test_tcp_broker_denies_out_of_policy(
    tcp_broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, auth, _ = tcp_broker

    with pytest.raises(BrokerDeniedError, match="not in the broker policy") as excinfo:
        backend.mint("exampleorg/forbidden", {"contents": "read"})

    assert excinfo.value.status_code == 403
    assert auth.mint_calls == []


def test_tcp_broker_status_and_describe(
    tcp_broker: tuple[BrokerBackend, _FakeAuth, io.StringIO],
) -> None:
    backend, _, _ = tcp_broker

    info = backend.describe()

    assert info["backend"] == "broker"
    assert info["broker_url"].startswith("http://127.0.0.1:")
    assert "broker_socket" not in info


def test_split_listen_validates() -> None:
    assert split_listen("0.0.0.0:8085") == ("0.0.0.0", 8085)
    for bad in ("8085", "host:", ":8085", "host:notaport", "host:0", "host:70000"):
        with pytest.raises(ConfigurationError, match="listen"):
            split_listen(bad)


def test_serve_requires_exactly_one_transport(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        serve(policy_path=str(tmp_path / "p.yaml"), auth=_FakeAuth())  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="exactly one"):
        serve(
            socket_path=str(tmp_path / "s.sock"),
            listen="127.0.0.1:0",
            policy_path=str(tmp_path / "p.yaml"),
            auth=_FakeAuth(),  # type: ignore[arg-type]
        )
