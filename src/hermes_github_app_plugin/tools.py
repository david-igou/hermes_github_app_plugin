"""Hermes tool handlers for GitHub App operations.

All handlers go through the token backend, so the gateway works identically
whether it mints locally or via the broker (GHAPP_BROKER_SOCKET / GHAPP_BROKER_URL).
Every mint is scoped to the target repository with the minimal permission set
for the operation.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .api import GitHubApi, repo_from_api_path
from .auth import GitHubAppAuth, requires_app_jwt
from .backends import BROKER_SOCKET_ENV, BrokerDeniedError, get_backend
from .config import ConfigurationError, load_config

_ISSUE_PERMISSIONS = {"issues": "write"}
_PR_PERMISSIONS = {"pull_requests": "write", "contents": "read"}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _error(exc: Exception) -> str:
    return _json({"success": False, "error": str(exc), "error_type": type(exc).__name__})


def _api() -> GitHubApi:
    backend = get_backend()
    info = backend.describe()
    return GitHubApi(backend, api_url=str(info.get("github_api_url", "https://api.github.com")))


def _handle_errors(fn: Any, *args: Any, **kwargs: Any) -> str:
    try:
        return _json({"success": True, **fn(*args, **kwargs)})
    except (
        ConfigurationError,
        BrokerDeniedError,
        httpx.HTTPError,
        KeyError,
        ValueError,
    ) as exc:
        return _error(exc)


def github_app_status(params: dict[str, Any], **_: Any) -> str:
    """Return GitHub App config status without printing secrets."""

    def run() -> dict[str, Any]:
        info = get_backend().describe()
        info.pop("private_key_source", None)  # not secret, but noise for the agent
        return {"configured": True, **info, "scope_management": "broker_policy_or_installation"}

    return _handle_errors(run)


def github_app_verify_identity(params: dict[str, Any], **_: Any) -> str:
    """Verify App identity and (optionally) scoped repository access."""

    def run() -> dict[str, Any]:
        repo = _repo(params)
        backend = get_backend()
        info = backend.describe()
        result: dict[str, Any] = {"backend": info}
        if not os.environ.get(BROKER_SOCKET_ENV, ""):
            auth = GitHubAppAuth(load_config())
            result["app"] = auth.app_request("GET", "/app")["result"]
        if repo:
            api = _api()
            result["repository_probe"] = api.request(
                "GET", f"/repos/{repo}", repo=repo, permissions={"contents": "read"}
            )
        return result

    return _handle_errors(run)


def github_app_api(params: dict[str, Any], **_: Any) -> str:
    """Call the GitHub REST API using a repo-scoped installation token."""

    def run() -> dict[str, Any]:
        method = str(params.get("method", "GET"))
        path = str(params["path"])
        repo = _repo(params) or repo_from_api_path(path)
        body = params.get("json_body")
        json_body = body if isinstance(body, dict) else None
        if requires_app_jwt(path):
            if os.environ.get(BROKER_SOCKET_ENV, ""):
                raise ConfigurationError(
                    "GitHub App JWT endpoints (/app...) are not available in broker mode"
                )
            return GitHubAppAuth(load_config()).app_request(method, path, json_body=json_body)
        if not repo:
            raise ConfigurationError(
                "repo (OWNER/REPO) is required so the token can be scoped to one repository"
            )
        return _api().request(method, path, repo=repo, json_body=json_body)

    return _handle_errors(run)


def github_app_graphql(params: dict[str, Any], **_: Any) -> str:
    """Call GitHub GraphQL using a repo-scoped installation token."""

    def run() -> dict[str, Any]:
        repo = _required_repo(params)
        variables = params.get("variables")
        return _api().graphql(
            str(params["query"]),
            variables if isinstance(variables, dict) else None,
            repo=repo,
        )

    return _handle_errors(run)


def github_app_create_issue(params: dict[str, Any], **_: Any) -> str:
    """Create an issue using the GitHub App identity."""

    def run() -> dict[str, Any]:
        repo = _required_repo(params)
        body: dict[str, Any] = {"title": str(params["title"])}
        if params.get("body") is not None:
            body["body"] = str(params["body"])
        labels = params.get("labels")
        if isinstance(labels, list):
            body["labels"] = labels
        assignees = params.get("assignees")
        if isinstance(assignees, list):
            body["assignees"] = assignees
        return _api().request(
            "POST",
            f"/repos/{repo}/issues",
            repo=repo,
            permissions=_ISSUE_PERMISSIONS,
            json_body=body,
        )

    return _handle_errors(run)


def github_app_comment_issue(params: dict[str, Any], **_: Any) -> str:
    """Comment on an issue or PR using the GitHub App identity."""

    def run() -> dict[str, Any]:
        repo = _required_repo(params)
        number = int(params["number"])
        return _api().request(
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            repo=repo,
            permissions=_ISSUE_PERMISSIONS,
            json_body={"body": str(params["body"])},
        )

    return _handle_errors(run)


def github_app_comment_pr(params: dict[str, Any], **kwargs: Any) -> str:
    """Comment on a pull request using the GitHub App identity."""
    return github_app_comment_issue(params, **kwargs)


def github_app_create_pr(params: dict[str, Any], **_: Any) -> str:
    """Create a pull request using the GitHub App identity."""

    def run() -> dict[str, Any]:
        repo = _required_repo(params)
        body = {
            "title": str(params["title"]),
            "head": str(params["head"]),
            "base": str(params["base"]),
            "body": str(params.get("body", "")),
            "draft": bool(params.get("draft", False)),
        }
        return _api().request(
            "POST",
            f"/repos/{repo}/pulls",
            repo=repo,
            permissions=_PR_PERMISSIONS,
            json_body=body,
        )

    return _handle_errors(run)


def _repo(params: dict[str, Any]) -> str | None:
    value = params.get("repo")
    return str(value) if value else None


def _required_repo(params: dict[str, Any]) -> str:
    repo = _repo(params)
    if not repo:
        raise ValueError("repo is required and must be in OWNER/NAME form")
    return repo
