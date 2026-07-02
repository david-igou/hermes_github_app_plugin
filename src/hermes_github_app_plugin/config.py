"""Configuration loading for the Hermes GitHub App plugin."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Owner key used when only a legacy single installation_id is configured.
ANY_OWNER = "*"

_DEFAULT_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
}


class ConfigurationError(RuntimeError):
    """Raised when GitHub App configuration is missing or invalid."""


@dataclass(frozen=True)
class KeySource:
    """Where the App private key comes from; resolved lazily at mint time."""

    kind: str  # "inline" | "path" | "cmd"
    value: str

    @property
    def display(self) -> str:
        """Safe description for status output (never key material)."""
        if self.kind == "inline":
            return "inline (GITHUB_APP_PRIVATE_KEY)"
        return f"{self.kind}: {self.value}"

    def resolve(self) -> str:
        """Return the PEM contents. May execute the configured command."""
        if self.kind == "inline":
            return self.value.replace("\\n", "\n")
        if self.kind == "path":
            path = Path(self.value).expanduser()
            if not path.exists():
                raise ConfigurationError(f"GitHub App private key file does not exist: {path}")
            return path.read_text(encoding="utf-8")
        result = subprocess.run(
            shlex.split(self.value), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise ConfigurationError(
                f"private_key_cmd failed (exit {result.returncode}): "
                f"{result.stderr.strip() or self.value}"
            )
        key = result.stdout.strip()
        if "PRIVATE KEY" not in key:
            raise ConfigurationError("private_key_cmd output does not look like a PEM key")
        return key + "\n"


@dataclass(frozen=True)
class GitHubAppConfig:
    """Per-agent GitHub App configuration."""

    client_id: str
    installations: Mapping[str, str]  # owner (lowercased) -> installation id
    key_source: KeySource
    app_slug: str | None = None
    github_api_url: str = "https://api.github.com"
    default_permissions: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_PERMISSIONS)
    )

    @property
    def private_key_source(self) -> str:
        return self.key_source.display

    def resolve_private_key(self) -> str:
        return self.key_source.resolve()

    def installation_for(self, owner: str | None) -> tuple[str, str]:
        """Return (owner, installation_id) for a repository owner.

        Falls back to the single configured installation when unambiguous.
        """
        if owner:
            found = self.installations.get(owner.lower())
            if found:
                return owner.lower(), found
        if not owner:
            if len(self.installations) == 1:
                return next(iter(self.installations.items()))
            raise ConfigurationError(
                "an owner (or OWNER/REPO) is required to pick an installation; "
                f"configured owners: {', '.join(sorted(self.installations))}"
            )
        if ANY_OWNER in self.installations:
            return owner.lower(), self.installations[ANY_OWNER]
        raise ConfigurationError(
            f"no installation configured for owner {owner!r}; "
            f"configured owners: {', '.join(sorted(self.installations))}"
        )


def hermes_home() -> Path:
    """Return the configured Hermes home directory."""
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def config_path() -> Path:
    """Return the config path: GHAPP_CONFIG override, else Hermes config.yaml."""
    override = os.environ.get("GHAPP_CONFIG", "")
    if override:
        return Path(override).expanduser()
    return hermes_home() / "config.yaml"


def _read_yaml_file() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, Mapping) else {}


def _read_config_yaml() -> Mapping[str, Any]:
    data = _read_yaml_file()
    section = data.get("github_app", {})
    if isinstance(section, Mapping):
        return section
    return {}


def write_github_app_config(values: Mapping[str, Any]) -> Path:
    """Merge GitHub App values into the config file and return the path written."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_yaml_file()
    section = data.get("github_app")
    if not isinstance(section, dict):
        section = {}
        data["github_app"] = section
    for key, value in values.items():
        if value in (None, "", (), [], {}):
            section.pop(key, None)
        else:
            section[key] = value
    # Remove legacy local allowlist keys if setup rewrites an older config.
    section.pop("allowed_repos", None)
    section.pop("allowed_owners", None)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    return path


def _parse_installations_env(raw: str) -> dict[str, str]:
    """Parse GITHUB_APP_INSTALLATIONS: "owner1=id1,owner2=id2"."""
    installations: dict[str, str] = {}
    for raw_pair in raw.split(","):
        pair = raw_pair.strip()
        if not pair:
            continue
        owner, sep, install_id = pair.partition("=")
        if not sep or not owner.strip() or not install_id.strip():
            raise ConfigurationError(
                f"invalid GITHUB_APP_INSTALLATIONS entry {pair!r}; expected owner=id[,owner=id]"
            )
        installations[owner.strip().lower()] = install_id.strip()
    return installations


def _read_installations(section: Mapping[str, Any]) -> dict[str, str]:
    env_raw = os.environ.get("GITHUB_APP_INSTALLATIONS", "")
    if env_raw:
        return _parse_installations_env(env_raw)

    raw = section.get("installations")
    if isinstance(raw, Mapping) and raw:
        return {str(owner).lower(): str(install_id) for owner, install_id in raw.items()}

    # Legacy single-installation config.
    legacy = os.environ.get("GITHUB_APP_INSTALLATION_ID") or str(section.get("installation_id", ""))
    if legacy:
        return {ANY_OWNER: legacy}
    raise ConfigurationError(
        "missing installations: set GITHUB_APP_INSTALLATIONS (owner=id,...), "
        "github_app.installations, or legacy GITHUB_APP_INSTALLATION_ID"
    )


def _read_key_source(section: Mapping[str, Any]) -> KeySource:
    inline = os.environ.get("GITHUB_APP_PRIVATE_KEY") or str(section.get("private_key", ""))
    if inline:
        return KeySource("inline", inline)
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or str(
        section.get("private_key_path", "")
    )
    if key_path:
        return KeySource("path", key_path)
    key_cmd = os.environ.get("GITHUB_APP_PRIVATE_KEY_CMD") or str(
        section.get("private_key_cmd", "")
    )
    if key_cmd:
        return KeySource("cmd", key_cmd)
    raise ConfigurationError(
        "missing GitHub App private key: set GITHUB_APP_PRIVATE_KEY, "
        "GITHUB_APP_PRIVATE_KEY_PATH, GITHUB_APP_PRIVATE_KEY_CMD, or the "
        "github_app.private_key_path / private_key_cmd config keys"
    )


def _read_default_permissions(section: Mapping[str, Any]) -> dict[str, str]:
    raw = section.get("default_permissions")
    if isinstance(raw, Mapping) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return dict(_DEFAULT_PERMISSIONS)


def load_config() -> GitHubAppConfig:
    """Load plugin configuration from environment variables and the config file."""
    section = _read_config_yaml()
    client_id = os.environ.get("GITHUB_APP_CLIENT_ID") or str(section.get("client_id", ""))
    if not client_id:
        raise ConfigurationError(
            "missing GitHub App client ID: set GITHUB_APP_CLIENT_ID or github_app.client_id"
        )
    return GitHubAppConfig(
        client_id=client_id,
        installations=_read_installations(section),
        key_source=_read_key_source(section),
        app_slug=os.environ.get("GITHUB_APP_SLUG") or section.get("app_slug"),
        github_api_url=os.environ.get("GITHUB_API_URL")
        or str(section.get("github_api_url", "https://api.github.com")).rstrip("/"),
        default_permissions=_read_default_permissions(section),
    )
