"""Runtime settings for one active local creator workspace."""

from __future__ import annotations

import getpass
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .runtime_paths import default_runtime_root
from .workspace import Workspace, default_data_home, load_active_workspace


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    repository_root: Path = REPOSITORY_ROOT
    workspace_slug: str = "default"
    workspace_name: str = "My creator workspace"
    platform: str = "douyin"
    profile_url: str = ""
    workspace_dir: Path | None = None
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_name: str = "csi_openbase"
    db_password: str = field(default="", repr=False)
    session_secret: str = field(
        default_factory=lambda: secrets.token_urlsafe(48), repr=False
    )
    browser_profile_dir: Path | None = None
    max_upload_bytes: int = 25 * 1024 * 1024
    job_poll_seconds: int = 5

    def __post_init__(self) -> None:
        if self.browser_profile_dir is None:
            session_root = (
                self.workspace_dir.parent.parent
                if self.workspace_dir is not None
                else default_runtime_root(self.repository_root)
            )
            object.__setattr__(
                self,
                "browser_profile_dir",
                session_root / "sessions" / self.workspace_slug,
            )

    @property
    def data_dir(self) -> Path:
        return self.workspace_dir or (
            default_runtime_root(self.repository_root)
            / "workspaces"
            / self.workspace_slug
        )

    @property
    def comments_dir(self) -> Path:
        return self.data_dir / "comments"

    @property
    def canonical_comments_path(self) -> Path:
        return self.comments_dir / "comments.jsonl"

    @property
    def targets_path(self) -> Path:
        return self.comments_dir / "collection-targets.json"

    @property
    def progress_path(self) -> Path:
        return self.comments_dir / "collection-progress.json"

    @property
    def schema_path(self) -> Path:
        return self.repository_root / "admin_app" / "resources" / "mysql-schema.sql"

    @property
    def batches_dir(self) -> Path:
        return self.comments_dir / "batches"

    @property
    def theme_rules_path(self) -> Path:
        return self.comments_dir / "theme-rules.json"

    @property
    def works_dir(self) -> Path:
        return self.data_dir / "works"

    @property
    def work_snapshots_path(self) -> Path:
        return self.works_dir / "work-snapshots.jsonl"

    @property
    def account_dir(self) -> Path:
        return self.data_dir / "account"

    @property
    def profile_snapshots_path(self) -> Path:
        return self.account_dir / "profile-snapshots.jsonl"

    @property
    def audience_snapshots_path(self) -> Path:
        return self.account_dir / "audience-snapshots.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


def resolve_database_password(*, prompt: bool = True) -> str:
    """Resolve the password without accepting a command-line value."""
    password = os.environ.get("CSI_OPENBASE_DB_PASSWORD")
    if password is None and prompt:
        password = getpass.getpass("MySQL password: ")
    if not password:
        raise RuntimeError(
            "MySQL password is required via hidden prompt or "
            "CSI_OPENBASE_DB_PASSWORD"
        )
    return password


def load_settings(*, prompt_for_password: bool = True) -> Settings:
    home = default_data_home()
    workspace: Workspace = load_active_workspace(home=home)

    def env(name: str, *, default: str) -> str:
        return os.environ.get(name) or default

    return Settings(
        workspace_slug=workspace.slug,
        workspace_name=workspace.display_name,
        platform=workspace.platform,
        profile_url=workspace.profile_url,
        workspace_dir=workspace.directory,
        browser_profile_dir=workspace.browser_profile_dir,
        web_host=env("CSI_OPENBASE_HOST", default="127.0.0.1"),
        web_port=int(
            env("CSI_OPENBASE_PORT", default="8000")
        ),
        db_host=env("CSI_OPENBASE_DB_HOST", default="127.0.0.1"),
        db_port=int(
            env("CSI_OPENBASE_DB_PORT", default="3306")
        ),
        db_user=env("CSI_OPENBASE_DB_USER", default="root"),
        db_name=workspace.database_name,
        db_password=resolve_database_password(prompt=prompt_for_password),
        session_secret=env(
            "CSI_OPENBASE_SESSION_SECRET", default=secrets.token_urlsafe(48)
        ),
    )
