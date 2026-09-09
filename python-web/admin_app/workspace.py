"""Local creator workspace registry.

CSI OpenBase is local-first: each workspace owns one creator account, one browser
profile, one archive directory, and one database.  Keeping that boundary at the
filesystem and database level prevents accidental cross-account reads while the
open-source application remains a single-operator tool.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runtime_paths import default_runtime_root, require_safe_runtime_path


WORKSPACE_SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PLATFORMS = frozenset({"douyin"})
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class WorkspaceError(ValueError):
    """Raised when workspace metadata or registry state is invalid."""


@dataclass(frozen=True, slots=True)
class Workspace:
    slug: str
    display_name: str
    platform: str
    database_name: str
    directory: Path
    profile_url: str = ""
    created_at: str = ""

    @property
    def manifest_path(self) -> Path:
        return self.directory / "workspace.json"

    @property
    def comments_dir(self) -> Path:
        return self.directory / "comments"

    @property
    def works_dir(self) -> Path:
        return self.directory / "works"

    @property
    def account_dir(self) -> Path:
        return self.directory / "account"

    @property
    def reports_dir(self) -> Path:
        return self.directory / "reports"

    @property
    def browser_profile_dir(self) -> Path:
        return self.directory.parent.parent / "sessions" / self.slug

    def as_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "slug": self.slug,
            "display_name": self.display_name,
            "platform": self.platform,
            "profile_url": self.profile_url,
            "database": {"name": self.database_name},
            "created_at": self.created_at,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def default_data_home() -> Path:
    configured = os.environ.get("CSI_OPENBASE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return default_runtime_root(PROJECT_ROOT)


def resolve_data_home(home: Path | None = None) -> Path:
    """Resolve the registry root without mixing runtime data with source files."""

    resolved = (home or default_data_home()).expanduser().resolve()
    try:
        return require_safe_runtime_path(
            resolved,
            project_root=PROJECT_ROOT,
            source_subdirectory=Path("var"),
            label="CSI_OPENBASE_HOME",
        )
    except ValueError as exc:
        raise WorkspaceError(
            "CSI OpenBase data inside the source repository must stay under its var directory"
        ) from exc


def validate_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise WorkspaceError(
            "workspace slug must start with a letter or digit and contain only "
            "lowercase letters, digits, or hyphens (maximum 48 characters)"
        )
    return slug


def validate_database_name(value: str) -> str:
    database_name = value.strip()
    if not DATABASE_RE.fullmatch(database_name):
        raise WorkspaceError(
            "database name must contain only letters, digits, or underscores, "
            "and cannot start with a digit"
        )
    return database_name


def _assert_database_name_unique(
    database_name: str,
    *,
    home: Path,
    workspace_slug: str,
) -> None:
    """Fail closed when another workspace manifest owns the same database."""
    normalized_database = validate_database_name(database_name)
    normalized_slug = validate_slug(workspace_slug)
    conflicts: list[str] = []
    for manifest_path in sorted((home.resolve() / "workspaces").glob("*/workspace.json")):
        if manifest_path.parent.name.casefold() == normalized_slug.casefold():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        database = payload.get("database")
        if not isinstance(database, dict):
            continue
        try:
            other_database = validate_database_name(str(database.get("name") or ""))
        except WorkspaceError:
            continue
        if other_database.casefold() == normalized_database.casefold():
            conflicts.append(manifest_path.parent.name)
    if conflicts:
        owners = ", ".join(conflicts)
        raise WorkspaceError(
            f"database {normalized_database!r} is already assigned to workspace: {owners}"
        )


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _create_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def workspace_directory(home: Path, slug: str) -> Path:
    return resolve_data_home(home) / "workspaces" / validate_slug(slug)


def active_workspace_slug(home: Path | None = None) -> str:
    explicit = os.environ.get("CSI_OPENBASE_WORKSPACE")
    if explicit:
        return validate_slug(explicit)
    registry_home = resolve_data_home(home)
    pointer = registry_home / "active-workspace"
    if not pointer.exists():
        raise WorkspaceError(
            "no active workspace; run `python scripts/manage_workspace.py create "
            "--slug <name> --display-name <name>` first"
        )
    return validate_slug(pointer.read_text(encoding="utf-8-sig").strip())


def load_workspace(slug: str, *, home: Path | None = None) -> Workspace:
    registry_home = resolve_data_home(home)
    normalized_slug = validate_slug(slug)
    directory = workspace_directory(registry_home, normalized_slug)
    manifest_path = directory / "workspace.json"
    if not manifest_path.exists():
        raise WorkspaceError(f"workspace does not exist: {normalized_slug}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read workspace manifest: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkspaceError("workspace manifest schema_version must be 1")
    if validate_slug(str(payload.get("slug") or "")) != normalized_slug:
        raise WorkspaceError("workspace manifest slug does not match its directory")
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name or len(display_name) > 120:
        raise WorkspaceError("workspace display_name must contain 1-120 characters")
    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise WorkspaceError(f"unsupported workspace platform: {platform!r}")
    database = payload.get("database")
    if not isinstance(database, dict):
        raise WorkspaceError("workspace database configuration must be an object")
    database_name = validate_database_name(str(database.get("name") or ""))
    return Workspace(
        slug=normalized_slug,
        display_name=display_name,
        platform=platform,
        database_name=database_name,
        directory=directory,
        profile_url=str(payload.get("profile_url") or "").strip(),
        created_at=str(payload.get("created_at") or "").strip(),
    )


def load_active_workspace(*, home: Path | None = None) -> Workspace:
    registry_home = resolve_data_home(home)
    workspace = load_workspace(active_workspace_slug(registry_home), home=registry_home)
    _assert_database_name_unique(
        workspace.database_name,
        home=registry_home,
        workspace_slug=workspace.slug,
    )
    return workspace


def activate_workspace(slug: str, *, home: Path | None = None) -> Workspace:
    registry_home = resolve_data_home(home)
    workspace = load_workspace(slug, home=registry_home)
    _assert_database_name_unique(
        workspace.database_name,
        home=registry_home,
        workspace_slug=workspace.slug,
    )
    _write_text_atomic(registry_home / "active-workspace", f"{workspace.slug}\n")
    return workspace


def initialize_workspace(
    *,
    slug: str,
    display_name: str,
    database_name: str | None = None,
    platform: str = "douyin",
    profile_url: str = "",
    home: Path | None = None,
    activate: bool = True,
) -> Workspace:
    registry_home = resolve_data_home(home)
    normalized_slug = validate_slug(slug)
    name = display_name.strip()
    if not name or len(name) > 120:
        raise WorkspaceError("display name must contain 1-120 characters")
    normalized_platform = platform.strip().lower()
    if normalized_platform not in SUPPORTED_PLATFORMS:
        raise WorkspaceError(f"unsupported platform: {normalized_platform!r}")
    default_database = f"csi_openbase_{normalized_slug.replace('-', '_')}"
    normalized_database = validate_database_name(database_name or default_database)
    directory = workspace_directory(registry_home, normalized_slug)
    manifest_path = directory / "workspace.json"
    if manifest_path.exists():
        raise WorkspaceError(f"workspace already exists: {normalized_slug}")
    _assert_database_name_unique(
        normalized_database,
        home=registry_home,
        workspace_slug=normalized_slug,
    )

    now = utc_now()
    workspace = Workspace(
        slug=normalized_slug,
        display_name=name,
        platform=normalized_platform,
        database_name=normalized_database,
        directory=directory,
        profile_url=profile_url.strip(),
        created_at=now,
    )
    for path in (
        workspace.comments_dir / "batches",
        workspace.works_dir / "imports",
        workspace.account_dir,
        workspace.reports_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    _create_private_directory(workspace.browser_profile_dir)

    _write_json_atomic(manifest_path, workspace.as_manifest())
    _write_text_atomic(workspace.comments_dir / "comments.jsonl", "")
    _write_json_atomic(
        workspace.comments_dir / "collection-targets.json",
        {
            "schema_version": 1,
            "scope_id": f"{normalized_slug}-default",
            "scope_note": "",
            "generated_at": now,
            "collections": [],
            "target_video_count": 0,
            "completed_video_count": 0,
            "videos": [],
        },
    )
    _write_json_atomic(
        workspace.comments_dir / "collection-progress.json",
        {
            "schema_version": 1,
            "scope_id": f"{normalized_slug}-default",
            "target_manifest": "comments/collection-targets.json",
            "target_video_count": 0,
            "completed_video_count": 0,
            "coverage_ratio": 0,
            "stored_record_count": 0,
            "updated_at": now,
            "videos": {},
        },
    )
    _write_json_atomic(
        workspace.comments_dir / "theme-rules.json",
        {"version": 1, "themes": []},
    )
    _write_text_atomic(workspace.works_dir / "work-snapshots.jsonl", "")
    _write_text_atomic(workspace.account_dir / "profile-snapshots.jsonl", "")
    _write_text_atomic(workspace.account_dir / "audience-snapshots.jsonl", "")
    if activate:
        _write_text_atomic(registry_home / "active-workspace", f"{normalized_slug}\n")
    return workspace


def list_workspaces(*, home: Path | None = None) -> list[Workspace]:
    registry_home = resolve_data_home(home)
    root = registry_home / "workspaces"
    if not root.exists():
        return []
    result: list[Workspace] = []
    for manifest in sorted(root.glob("*/workspace.json")):
        try:
            result.append(load_workspace(manifest.parent.name, home=registry_home))
        except WorkspaceError:
            continue
    return result
