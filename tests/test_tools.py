"""Tests for the Hermes tool handlers (backend-routed)."""

from __future__ import annotations

import json

import pytest

from hermes_github_app_plugin import tools
from tests.conftest import FakeBackend


class FakeApi:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.requests: list[tuple[str, str, str, dict[str, str] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        repo: str,
        permissions: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.requests.append((method, path, repo, permissions))
        return {"status_code": 200, "result": {"ok": True}}

    def graphql(
        self,
        query: str,
        variables: dict[str, object] | None = None,
        *,
        repo: str,
        permissions: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.requests.append(("GRAPHQL", query, repo, permissions))
        return {"status_code": 200, "result": {"data": {}}}


@pytest.fixture()
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    backend = FakeBackend()
    api = FakeApi(backend)
    monkeypatch.setattr(tools, "get_backend", lambda: backend)
    monkeypatch.setattr(tools, "_api", lambda: api)
    return api


def test_status_reports_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(tools, "get_backend", lambda: backend)

    result = json.loads(tools.github_app_status({}))

    assert result["success"] is True
    assert result["backend"] == "fake"
    assert result["app_slug"] == "test-agent"


def test_api_infers_repo_and_scopes(fake_api: FakeApi) -> None:
    result = json.loads(tools.github_app_api({"path": "/repos/OWNER/REPO/issues"}))

    assert result["success"] is True
    assert fake_api.requests[0][1] == "/repos/OWNER/REPO/issues"
    assert fake_api.requests[0][2] == "OWNER/REPO"


def test_api_requires_repo_for_unscoped_paths(fake_api: FakeApi) -> None:
    result = json.loads(tools.github_app_api({"path": "/user"}))

    assert result["success"] is False
    assert "repo (OWNER/REPO) is required" in result["error"]


def test_graphql_requires_repo(fake_api: FakeApi) -> None:
    result = json.loads(tools.github_app_graphql({"query": "query {}"}))

    assert result["success"] is False

    ok = json.loads(tools.github_app_graphql({"query": "query {}", "repo": "o/r"}))
    assert ok["success"] is True
    assert fake_api.requests[-1] == ("GRAPHQL", "query {}", "o/r", None)


def test_create_issue_uses_issue_permissions(fake_api: FakeApi) -> None:
    result = json.loads(
        tools.github_app_create_issue({"repo": "o/r", "title": "t", "labels": ["bug"]})
    )

    assert result["success"] is True
    method, path, repo, permissions = fake_api.requests[0]
    assert (method, path, repo) == ("POST", "/repos/o/r/issues", "o/r")
    assert permissions == {"issues": "write"}


def test_create_pr_uses_pr_permissions(fake_api: FakeApi) -> None:
    result = json.loads(
        tools.github_app_create_pr({"repo": "o/r", "title": "t", "head": "feature", "base": "main"})
    )

    assert result["success"] is True
    _, path, _, permissions = fake_api.requests[0]
    assert path == "/repos/o/r/pulls"
    assert permissions == {"pull_requests": "write", "contents": "read"}
