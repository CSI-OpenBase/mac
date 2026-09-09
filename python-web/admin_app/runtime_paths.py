"""Runtime locations that remain writable outside an installed package."""

from __future__ import annotations

import os
import sys
from pathlib import Path


SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[1]
IS_SOURCE_CHECKOUT = (SOURCE_PROJECT_ROOT / "pyproject.toml").is_file()


def user_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base).expanduser() if base else Path.home() / "AppData" / "Local"
        return (root / "CSI OpenBase").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "CSI OpenBase").resolve()

    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return (root / "csi-openbase").resolve()


def default_runtime_root(project_root: Path) -> Path:
    if IS_SOURCE_CHECKOUT:
        return (project_root / "var").resolve()
    return user_data_root()


def require_safe_runtime_path(
    path: Path,
    *,
    project_root: Path,
    source_subdirectory: Path,
    label: str,
) -> Path:
    resolved = path.expanduser().resolve()
    resolved_project = project_root.resolve()
    if resolved != resolved_project and not resolved.is_relative_to(resolved_project):
        return resolved

    allowed = (resolved_project / source_subdirectory).resolve()
    if IS_SOURCE_CHECKOUT and resolved.is_relative_to(allowed):
        return resolved

    raise ValueError(
        f"{label} must not be stored inside the CSI OpenBase code directory; "
        f"source checkouts may use {allowed}"
    )
