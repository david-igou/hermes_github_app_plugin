"""Tests for the CLI: setup, credential helper, wrappers, token/api routing."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest
import yaml

from hermes_github_app_plugin import cli
from hermes_github_app_plugin.config import ConfigurationError
from tests.conftest import PRIVATE_KEY, FakeBackend


def _setup_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "github_app_command": "setup",
        "client_id": "Iv1.exampleclientid",
        "installations": "ExampleOrg=111,exampleuser=222",
        "private_key_path": None,
        "private_key_cmd": None,
        "app_slug": "test-agent",
        "non_interactive": True,
        "repo": None,
        "skip_verify": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_setup_non_interactive_writes_installations_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hermes_home = tmp_path / ".hermes"
    key_path = tmp_path / "app.pem"
    key_path.write_text(PRIVATE_KEY, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert cli.main(_setup_args(private_key_path=str(key_path))) == 0

    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert data["github_app"] == {
        "client_id": "Iv1.exampleclientid",
        "installations": {"exampleorg": "111", "exampleuser": "222"},
        "private_key_path": str(key_path),
        "app_slug": "test-agent",
    }
    assert "Skipped verification" in capsys.readouterr().out


def test_setup_accepts_private_key_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = cli.main(
        _setup_args(private_key_cmd="op read op://claude/igou-dev-github-app/private_key")
    )

    assert result == 0
    data = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert (
        data["github_app"]["private_key_cmd"]
        == "op read op://claude/igou-dev-github-app/private_key"
    )
    assert "private_key_path" not in data["github_app"]


def test_setup_requires_some_key_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    assert cli.main(_setup_args()) == 1
    assert "private-key-path or --private-key-cmd" in capsys.readouterr().err


def test_doctor_skip_network_reports_local_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hermes_home = tmp_path / ".hermes"
    key_path = tmp_path / "app.pem"
    key_path.write_text(PRIVATE_KEY, encoding="utf-8")
    key_path.chmod(0o600)
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        f"""
github_app:
  client_id: Iv1.exampleclientid
  installations:
    exampleorg: 111
  private_key_path: {key_path}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/usr/bin/{command}")

    assert cli._doctor(None, skip_network=True) == 0

    output = capsys.readouterr().out
    assert "✓ GitHub App config loaded: installations: exampleorg" in output
    assert "✓ private key file permissions: 0o600" in output
    assert "✓ git-credential-ghapp on PATH" in output
    assert "Local doctor passed" in output


def test_token_requires_repo(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(github_app_command="token", repo=None, permission=[], json=False)

    assert cli.main(args) == 1
    assert "requires --repo" in capsys.readouterr().err


def test_token_mints_scoped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(cli, "get_backend", lambda: backend)
    args = argparse.Namespace(
        github_app_command="token",
        repo="exampleorg/repo",
        permission=["contents=write", "issues=read"],
        json=False,
    )

    assert cli.main(args) == 0

    assert capsys.readouterr().out.strip() == backend.token.token
    assert backend.mint_calls == [("exampleorg/repo", {"contents": "write", "issues": "read"})]


def test_parse_permissions_rejects_bad_pairs() -> None:
    with pytest.raises(ConfigurationError, match="invalid --permission"):
        cli._parse_permissions(["contents"])


def test_credential_helper_get_mints_for_repo_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(cli, "get_backend", lambda: backend)
    monkeypatch.setattr(cli.sys, "argv", ["git-credential-ghapp", "get"])
    monkeypatch.setattr(
        cli.sys,
        "stdin",
        io.StringIO("protocol=https\nhost=github.com\npath=ExampleOrg/repo.git\n\n"),
    )

    assert cli.git_credential_main() == 0

    output = capsys.readouterr().out
    assert "username=x-access-token" in output
    assert f"password={backend.token.token}" in output
    assert backend.mint_calls == [("ExampleOrg/repo", {"contents": "write"})]


def test_credential_helper_ignores_other_hosts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(cli, "get_backend", lambda: backend)
    monkeypatch.setattr(cli.sys, "argv", ["git-credential-ghapp", "get"])
    monkeypatch.setattr(
        cli.sys, "stdin", io.StringIO("protocol=https\nhost=gitlab.com\npath=o/r.git\n\n")
    )

    assert cli.git_credential_main() == 0

    assert capsys.readouterr().out == ""
    assert backend.mint_calls == []


def test_credential_helper_requires_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["git-credential-ghapp", "get"])
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))

    assert cli.git_credential_main() == 1
    assert "useHttpPath" in capsys.readouterr().err


def test_credential_helper_store_and_erase_are_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for action in ("store", "erase"):
        monkeypatch.setattr(cli.sys, "argv", ["git-credential-ghapp", action])
        assert cli.git_credential_main() == 0


def test_extract_wrapper_args() -> None:
    repo, permissions, child = cli._extract_wrapper_args(
        ["--repo", "owner/repo", "--permission", "contents=read", "--", "pr", "list"]
    )

    assert repo == "owner/repo"
    assert permissions == {"contents": "read"}
    assert child == ["pr", "list"]


def test_extract_wrapper_args_requires_child_command() -> None:
    with pytest.raises(ConfigurationError, match="missing command"):
        cli._extract_wrapper_args(["--repo", "owner/repo"])


def test_api_infers_repo_from_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(cli, "get_backend", lambda: backend)

    class FakeApi:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
            return {"status_code": 200, "result": {"path": path, "repo": kwargs.get("repo")}}

    monkeypatch.setattr(cli, "GitHubApi", FakeApi)

    assert cli._api("GET", "/repos/OWNER/REPO/issues", repo=None, body=None) == 0

    assert '"repo": "OWNER/REPO"' in capsys.readouterr().out


def test_api_app_paths_refused_in_broker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHAPP_BROKER_SOCKET", "/run/ghbroker/ghbroker.sock")

    with pytest.raises(ConfigurationError, match="not available in broker mode"):
        cli._api("GET", "/app", repo=None, body=None)


def test_serve_refuses_broker_recursion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHAPP_BROKER_SOCKET", "/run/ghbroker/ghbroker.sock")

    with pytest.raises(ConfigurationError, match="refusing to serve"):
        cli._serve("/tmp/x.sock", "/tmp/policy.yaml", "0660")
