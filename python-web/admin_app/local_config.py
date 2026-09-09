"""Configuration for the file-first CSI OpenBase desktop workflow."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .runtime_paths import default_runtime_root, require_safe_runtime_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_path(value: str | None, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default.resolve()


def _workspace_session_key(data_home: Path) -> str:
    normalized = os.path.normcase(str(data_home.resolve())).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _require_path_within(path: Path, root: Path, *, label: str) -> None:
    """Reject existing links or junctions that resolve outside their root."""
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(
            f"{label} resolves outside its configured root: {resolved_path}"
        )


@dataclass(frozen=True, slots=True)
class LocalSettings:
    data_home: Path
    session_home: Path
    host: str = "127.0.0.1"
    port: int = 8000
    desktop_token: str = field(default="", repr=False)
    instance_nonce: str = field(default="", repr=False)
    session_secret: str = field(
        default_factory=lambda: secrets.token_urlsafe(48), repr=False
    )
    authorization_timeout_seconds: int = 300
    browser_capture_seconds: int = 120

    @property
    def database_path(self) -> Path:
        return self.data_home / "openbase.sqlite3"

    @property
    def exports_dir(self) -> Path:
        return self.data_home / "exports"

    @property
    def works_dir(self) -> Path:
        return self.data_home / "works"

    @property
    def discovery_dir(self) -> Path:
        return self.works_dir / "discovery"

    @property
    def videos_dir(self) -> Path:
        return self.works_dir / "videos"

    @property
    def browser_profile_dir(self) -> Path:
        return self.session_home / "browser-profile"

    @property
    def log_dir(self) -> Path:
        return self.data_home / "logs"

    def ensure_directories(self) -> None:
        data_paths = (
            (self.database_path, "database"),
            (self.exports_dir, "exports directory"),
            (self.works_dir, "works directory"),
            (self.discovery_dir, "discovery directory"),
            (self.videos_dir, "videos directory"),
            (self.log_dir, "log directory"),
        )
        session_paths = ((self.browser_profile_dir, "browser profile directory"),)

        # Preflight every destination before creating anything. Path.resolve()
        # follows both symlinks and Windows directory junctions.
        for path, label in data_paths:
            _require_path_within(path, self.data_home, label=label)
        for path, label in session_paths:
            _require_path_within(path, self.session_home, label=label)

        for directory in (
            self.data_home,
            self.exports_dir,
            self.discovery_dir,
            self.videos_dir,
            self.browser_profile_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def load_local_settings() -> LocalSettings:
    data_home = _resolve_path(
        os.environ.get("CSI_OPENBASE_HOME"),
        default_runtime_root(PROJECT_ROOT) / "local",
    )
    data_home = require_safe_runtime_path(
        data_home,
        project_root=PROJECT_ROOT,
        source_subdirectory=Path("var"),
        label="CSI_OPENBASE_HOME",
    )
    configured_session_home = os.environ.get("CSI_OPENBASE_SESSION_HOME")
    if os.environ.get("CSI_OPENBASE_DESKTOP_TOKEN") and os.environ.get("LOCALAPPDATA"):
        session_home = (
            Path(os.environ["LOCALAPPDATA"])
            / "CSI OpenBase"
            / "sessions"
            / _workspace_session_key(data_home)
        ).resolve()
    elif configured_session_home:
        session_home = _resolve_path(configured_session_home, data_home / ".sessions")
    elif data_home.is_relative_to(PROJECT_ROOT):
        session_home = (
            PROJECT_ROOT
            / "var"
            / "sessions"
            / _workspace_session_key(data_home)
        ).resolve()
    else:
        session_home = (data_home / ".sessions").resolve()

    session_home = require_safe_runtime_path(
        session_home,
        project_root=PROJECT_ROOT,
        source_subdirectory=Path("var") / "sessions",
        label="CSI_OPENBASE_SESSION_HOME",
    )

    settings = LocalSettings(
        data_home=data_home,
        session_home=session_home,
        host=os.environ.get("CSI_OPENBASE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CSI_OPENBASE_PORT", "8000")),
        desktop_token=os.environ.get("CSI_OPENBASE_DESKTOP_TOKEN", ""),
        instance_nonce=os.environ.get("CSI_OPENBASE_INSTANCE_NONCE", ""),
        authorization_timeout_seconds=int(
            os.environ.get("CSI_OPENBASE_AUTH_TIMEOUT", "300")
        ),
        browser_capture_seconds=int(
            os.environ.get("CSI_OPENBASE_CAPTURE_SECONDS", "120")
        ),
    )
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("the local archive app may only listen on a loopback address")
    if not 1 <= settings.port <= 65535:
        raise ValueError("CSI_OPENBASE_PORT must be between 1 and 65535")
    settings.ensure_directories()
    return settings
