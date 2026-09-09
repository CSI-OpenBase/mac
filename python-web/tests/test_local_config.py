from __future__ import annotations

from pathlib import Path

import pytest

import admin_app.local_config as local_config_module
import admin_app.runtime_paths as runtime_paths
from admin_app.local_config import LocalSettings, load_local_settings


def _symlink_or_skip(link: Path, target: Path, *, is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are not available in this environment: {exc}")


def test_desktop_browser_sessions_are_isolated_by_work_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("CSI_OPENBASE_DESKTOP_TOKEN", "desktop-token")
    monkeypatch.setenv(
        "CSI_OPENBASE_SESSION_HOME", str(tmp_path / "ambient-shared-session")
    )

    first_home = tmp_path / "creator-one"
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(first_home))
    first = load_local_settings()
    repeated = load_local_settings()

    monkeypatch.setenv("CSI_OPENBASE_HOME", str(tmp_path / "creator-two"))
    second = load_local_settings()

    expected_parent = (tmp_path / "local-app-data" / "CSI OpenBase" / "sessions").resolve()
    assert first.session_home.parent == expected_parent
    assert first.session_home == repeated.session_home
    assert first.session_home != second.session_home


def test_installed_package_defaults_to_the_user_data_directory(
    tmp_path: Path, monkeypatch
) -> None:
    package_root = tmp_path / "site-packages"
    user_data = tmp_path / "user-data"
    monkeypatch.setattr(local_config_module, "PROJECT_ROOT", package_root)
    monkeypatch.setattr(runtime_paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(user_data))
    monkeypatch.delenv("CSI_OPENBASE_HOME", raising=False)
    monkeypatch.delenv("CSI_OPENBASE_SESSION_HOME", raising=False)
    monkeypatch.delenv("CSI_OPENBASE_DESKTOP_TOKEN", raising=False)

    settings = load_local_settings()

    expected = (user_data / "csi-openbase" / "local").resolve()
    assert settings.data_home == expected
    assert settings.session_home == expected / ".sessions"
    assert settings.database_path.parent == expected


def test_rejects_local_archive_data_inside_source_outside_var(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "source"
    monkeypatch.setattr(local_config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(runtime_paths, "IS_SOURCE_CHECKOUT", True)
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(project_root / "creator-data"))
    monkeypatch.delenv("CSI_OPENBASE_DESKTOP_TOKEN", raising=False)

    with pytest.raises(ValueError, match="must not be stored inside"):
        load_local_settings()

    assert not (project_root / "creator-data").exists()


def test_rejects_local_archive_session_inside_source_outside_var_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "source"
    monkeypatch.setattr(local_config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(runtime_paths, "IS_SOURCE_CHECKOUT", True)
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(project_root / "var" / "local"))
    monkeypatch.setenv(
        "CSI_OPENBASE_SESSION_HOME", str(project_root / ".sessions")
    )
    monkeypatch.delenv("CSI_OPENBASE_DESKTOP_TOKEN", raising=False)

    with pytest.raises(ValueError, match="CSI_OPENBASE_SESSION_HOME"):
        load_local_settings()

    assert not (project_root / ".sessions").exists()


@pytest.mark.parametrize(
    ("relative_path", "is_directory"),
    [
        (Path("exports"), True),
        (Path("works"), True),
        (Path("works/discovery"), True),
        (Path("works/videos"), True),
        (Path("logs"), True),
        (Path("openbase.sqlite3"), False),
    ],
)
def test_rejects_data_path_links_outside_selected_home_before_creating_directories(
    tmp_path: Path, relative_path: Path, is_directory: bool
) -> None:
    data_home = tmp_path / "data"
    session_home = tmp_path / "sessions"
    outside = tmp_path / ("outside-dir" if is_directory else "outside.sqlite3")
    data_home.mkdir()
    if is_directory:
        outside.mkdir()
    else:
        outside.touch()

    link = data_home / relative_path
    link.parent.mkdir(parents=True, exist_ok=True)
    _symlink_or_skip(link, outside, is_directory=is_directory)

    settings = LocalSettings(data_home=data_home, session_home=session_home)
    with pytest.raises(ValueError, match="outside its configured root"):
        settings.ensure_directories()

    assert not settings.browser_profile_dir.exists()


def test_rejects_browser_profile_link_outside_session_home_before_writing_data(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "data"
    session_home = tmp_path / "sessions"
    outside = tmp_path / "outside-profile"
    session_home.mkdir()
    outside.mkdir()
    _symlink_or_skip(
        session_home / "browser-profile", outside, is_directory=True
    )

    settings = LocalSettings(data_home=data_home, session_home=session_home)
    with pytest.raises(ValueError, match="outside its configured root"):
        settings.ensure_directories()

    assert not data_home.exists()


def test_allows_child_link_that_resolves_inside_selected_home(tmp_path: Path) -> None:
    data_home = tmp_path / "data"
    session_home = tmp_path / "sessions"
    internal_exports = data_home / "storage" / "exports"
    internal_exports.mkdir(parents=True)
    _symlink_or_skip(data_home / "exports", internal_exports, is_directory=True)

    settings = LocalSettings(data_home=data_home, session_home=session_home)
    settings.ensure_directories()

    assert settings.exports_dir.resolve() == internal_exports.resolve()
    assert settings.database_path.parent == data_home
    assert settings.browser_profile_dir.is_dir()


def test_allows_configured_roots_that_are_links(tmp_path: Path) -> None:
    real_data_home = tmp_path / "real-data"
    real_session_home = tmp_path / "real-sessions"
    real_data_home.mkdir()
    real_session_home.mkdir()
    data_home = tmp_path / "data-link"
    session_home = tmp_path / "session-link"
    _symlink_or_skip(data_home, real_data_home, is_directory=True)
    _symlink_or_skip(session_home, real_session_home, is_directory=True)

    settings = LocalSettings(data_home=data_home, session_home=session_home)
    settings.ensure_directories()

    assert settings.exports_dir.resolve().is_relative_to(real_data_home.resolve())
    assert settings.browser_profile_dir.resolve().is_relative_to(
        real_session_home.resolve()
    )
