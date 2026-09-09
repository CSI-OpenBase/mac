from __future__ import annotations

import json
from pathlib import Path

import pytest

import admin_app.workspace as workspace_module
import admin_app.runtime_paths as runtime_paths
from admin_app.config import Settings, load_settings
from admin_app.database import DatabaseConfig
from admin_app.work_data import load_work_snapshots, normalize_work_snapshot, write_work_snapshots
from admin_app.workspace import (
    WorkspaceError,
    activate_workspace,
    default_data_home,
    initialize_workspace,
    list_workspaces,
    load_active_workspace,
    load_workspace,
)


def test_workspace_creation_listing_and_activation_are_deterministic(
    tmp_path: Path,
) -> None:
    first = initialize_workspace(
        slug="Primary-Account",
        display_name="主账号",
        profile_url="https://www.douyin.com/user/primary",
        home=tmp_path,
    )

    assert first.slug == "primary-account"
    assert first.database_name == "csi_openbase_primary_account"
    assert first.manifest_path.is_file()
    assert first.browser_profile_dir.is_dir()
    assert first.browser_profile_dir == tmp_path / "sessions" / "primary-account"
    assert (first.comments_dir / "comments.jsonl").is_file()
    assert load_workspace(first.slug, home=tmp_path) == first
    assert load_active_workspace(home=tmp_path).slug == first.slug

    second = initialize_workspace(
        slug="second-account",
        display_name="第二账号",
        home=tmp_path,
        activate=False,
    )
    assert load_active_workspace(home=tmp_path).slug == first.slug
    assert [workspace.slug for workspace in list_workspaces(home=tmp_path)] == [
        "primary-account",
        "second-account",
    ]

    activated = activate_workspace(second.slug, home=tmp_path)

    assert activated == second
    assert load_active_workspace(home=tmp_path).slug == second.slug
    with pytest.raises(WorkspaceError, match="already exists"):
        initialize_workspace(
            slug=first.slug,
            display_name="重复账号",
            home=tmp_path,
        )


def test_workspaces_with_the_same_external_work_id_use_isolated_files(
    tmp_path: Path,
) -> None:
    first = initialize_workspace(
        slug="creator-a", display_name="创作者 A", home=tmp_path
    )
    second = initialize_workspace(
        slug="creator-b", display_name="创作者 B", home=tmp_path
    )
    first_settings = Settings(
        workspace_slug=first.slug,
        workspace_name=first.display_name,
        workspace_dir=first.directory,
        db_name=first.database_name,
    )
    second_settings = Settings(
        workspace_slug=second.slug,
        workspace_name=second.display_name,
        workspace_dir=second.directory,
        db_name=second.database_name,
    )
    shared_id = "7654321098765432109"
    common = {
        "work_id": shared_id,
        "published_at": "2026-09-01T08:00:00+08:00",
        "observed_at": "2026-09-06T08:00:00+08:00",
        "view_count": 100,
    }
    first_record = normalize_work_snapshot({**common, "title": "A 的作品"})
    second_record = normalize_work_snapshot({**common, "title": "B 的作品"})

    write_work_snapshots(first_settings.work_snapshots_path, [first_record])
    write_work_snapshots(second_settings.work_snapshots_path, [second_record])
    first_settings.canonical_comments_path.write_text(
        '{"workspace":"a","external_id":"same"}\n', encoding="utf-8"
    )
    second_settings.canonical_comments_path.write_text(
        '{"workspace":"b","external_id":"same"}\n', encoding="utf-8"
    )

    assert first_settings.data_dir != second_settings.data_dir
    assert first_settings.db_name != second_settings.db_name
    assert load_work_snapshots(first_settings.work_snapshots_path)[0]["title"] == "A 的作品"
    assert load_work_snapshots(second_settings.work_snapshots_path)[0]["title"] == "B 的作品"
    assert '"workspace":"a"' in first_settings.canonical_comments_path.read_text(
        encoding="utf-8"
    )
    assert '"workspace":"b"' in second_settings.canonical_comments_path.read_text(
        encoding="utf-8"
    )


def test_workspace_creation_rejects_database_name_owned_by_another_workspace(
    tmp_path: Path,
) -> None:
    first = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        database_name="Shared_Creator_Data",
        home=tmp_path,
    )

    with pytest.raises(WorkspaceError, match=r"already assigned.*creator-a"):
        initialize_workspace(
            slug="creator-b",
            display_name="Creator B",
            database_name=first.database_name.lower(),
            home=tmp_path,
        )

    assert not (tmp_path / "workspaces" / "creator-b").exists()


def test_duplicate_database_from_tampered_manifest_cannot_be_activated_or_loaded(
    tmp_path: Path,
) -> None:
    first = initialize_workspace(
        slug="creator-a", display_name="Creator A", home=tmp_path
    )
    second = initialize_workspace(
        slug="creator-b", display_name="Creator B", home=tmp_path, activate=False
    )
    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["name"] = first.database_name
    second.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(WorkspaceError, match=r"already assigned.*creator-a"):
        activate_workspace(second.slug, home=tmp_path)
    assert (tmp_path / "active-workspace").read_text(encoding="utf-8").strip() == first.slug

    (tmp_path / "active-workspace").write_text(f"{second.slug}\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match=r"already assigned.*creator-a"):
        load_active_workspace(home=tmp_path)


def test_load_settings_uses_manifest_database_despite_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        database_name="manifest_database",
        home=tmp_path,
    )
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(tmp_path))
    monkeypatch.delenv("CSI_OPENBASE_WORKSPACE", raising=False)
    monkeypatch.setenv("CSI_OPENBASE_DB_NAME", "unexpected_override")
    monkeypatch.setenv("CSI_OPENBASE_DB_PASSWORD", "test-password")

    settings = load_settings(prompt_for_password=False)

    assert settings.db_name == workspace.database_name


def test_default_workspace_data_home_is_repository_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("CSI_OPENBASE_HOME", raising=False)

    assert default_data_home() == (tmp_path / "var").resolve()

    workspace = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        home=tmp_path / "var",
    )

    assert workspace.directory == (tmp_path / "var" / "workspaces" / "creator-a")
    assert workspace.browser_profile_dir == (
        tmp_path / "var" / "sessions" / "creator-a"
    ).resolve()


def test_installed_dashboard_defaults_to_the_user_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "PROJECT_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(runtime_paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.delenv("CSI_OPENBASE_HOME", raising=False)

    assert default_data_home() == (
        tmp_path / "user-data" / "csi-openbase"
    ).resolve()

    settings = Settings(repository_root=tmp_path / "site-packages")
    assert settings.data_dir == (
        tmp_path / "user-data" / "csi-openbase" / "workspaces" / "default"
    ).resolve()
    assert settings.browser_profile_dir == (
        tmp_path / "user-data" / "csi-openbase" / "sessions" / "default"
    ).resolve()


def test_workspace_data_inside_repository_is_limited_to_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "PROJECT_ROOT", tmp_path)

    with pytest.raises(WorkspaceError, match="must stay under its var directory"):
        initialize_workspace(
            slug="creator-a",
            display_name="Creator A",
            home=tmp_path / "workspace-data",
        )

    assert not (tmp_path / "workspace-data").exists()


def test_custom_data_home_keeps_workspace_and_session_under_one_root(
    tmp_path: Path,
) -> None:
    custom_home = tmp_path / "creator-data"

    workspace = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        home=custom_home,
    )

    assert workspace.directory == custom_home / "workspaces" / "creator-a"
    assert workspace.browser_profile_dir == custom_home / "sessions" / "creator-a"


def test_only_openbase_workspace_environment_is_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openbase_home = tmp_path / "openbase"
    legacy_home = tmp_path / "legacy"
    repository_root = tmp_path / "repository"
    fallback_home = repository_root / "var"
    openbase_workspace = initialize_workspace(
        slug="openbase-account",
        display_name="OpenBase account",
        home=openbase_home,
    )
    initialize_workspace(
        slug="legacy-account",
        display_name="Legacy account",
        home=legacy_home,
    )
    fallback_workspace = initialize_workspace(
        slug="fallback-account",
        display_name="Fallback account",
        home=fallback_home,
    )
    monkeypatch.setattr(workspace_module, "PROJECT_ROOT", repository_root)
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(openbase_home))
    monkeypatch.setenv("CSI_HUB_HOME", str(legacy_home))
    monkeypatch.setenv("CSI_OPENBASE_WORKSPACE", "openbase-account")
    monkeypatch.setenv("CSI_HUB_WORKSPACE", "legacy-account")

    assert default_data_home() == openbase_home.resolve()
    assert load_active_workspace() == openbase_workspace

    monkeypatch.delenv("CSI_OPENBASE_HOME", raising=False)
    monkeypatch.delenv("CSI_OPENBASE_WORKSPACE", raising=False)

    assert default_data_home() == fallback_home.resolve()
    assert load_active_workspace() == fallback_workspace


def test_only_openbase_runtime_environment_is_effective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        database_name="manifest_database",
        home=tmp_path,
    )
    environment = {
        "CSI_OPENBASE_HOME": str(tmp_path),
        "CSI_OPENBASE_HOST": "openbase-host",
        "CSI_HUB_HOST": "hub-host",
        "DOUYIN_ADMIN_HOST": "douyin-host",
        "CSI_OPENBASE_PORT": "8100",
        "CSI_HUB_PORT": "8200",
        "DOUYIN_ADMIN_PORT": "8300",
        "CSI_OPENBASE_DB_HOST": "openbase-db",
        "CSI_HUB_DB_HOST": "hub-db",
        "DOUYIN_DB_HOST": "douyin-db",
        "CSI_OPENBASE_DB_PORT": "3307",
        "CSI_HUB_DB_PORT": "3308",
        "DOUYIN_DB_PORT": "3309",
        "CSI_OPENBASE_DB_USER": "openbase-user",
        "CSI_HUB_DB_USER": "hub-user",
        "DOUYIN_DB_USER": "douyin-user",
        "CSI_OPENBASE_DB_PASSWORD": "openbase-password",
        "CSI_HUB_DB_PASSWORD": "hub-password",
        "DOUYIN_DB_PASSWORD": "douyin-password",
        "CSI_OPENBASE_SESSION_SECRET": "openbase-session",
        "CSI_HUB_SESSION_SECRET": "hub-session",
        "DOUYIN_ADMIN_SESSION_SECRET": "douyin-session",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CSI_OPENBASE_WORKSPACE", raising=False)
    monkeypatch.setenv("CSI_HUB_WORKSPACE", "ignored-legacy-workspace")

    settings = load_settings(prompt_for_password=False)

    assert settings.workspace_slug == workspace.slug
    assert settings.web_host == "openbase-host"
    assert settings.web_port == 8100
    assert settings.db_host == "openbase-db"
    assert settings.db_port == 3307
    assert settings.db_user == "openbase-user"
    assert settings.db_name == workspace.database_name
    assert settings.db_password == "openbase-password"
    assert settings.session_secret == "openbase-session"


def test_legacy_runtime_environment_names_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = initialize_workspace(
        slug="creator-a",
        display_name="Creator A",
        database_name="manifest_database",
        home=tmp_path,
    )
    monkeypatch.setenv("CSI_OPENBASE_HOME", str(tmp_path))
    for name in (
        "CSI_OPENBASE_HOST",
        "CSI_OPENBASE_PORT",
        "CSI_OPENBASE_DB_HOST",
        "CSI_OPENBASE_DB_PORT",
        "CSI_OPENBASE_DB_USER",
        "CSI_OPENBASE_DB_PASSWORD",
        "CSI_OPENBASE_SESSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CSI_OPENBASE_WORKSPACE", raising=False)

    legacy_environment = {
        "CSI_HUB_HOME": str(tmp_path / "ignored-home"),
        "CSI_HUB_WORKSPACE": "ignored-workspace",
        "CSI_HUB_HOST": "hub-host",
        "CSI_HUB_PORT": "8200",
        "CSI_HUB_DB_HOST": "hub-db",
        "CSI_HUB_DB_PORT": "3308",
        "CSI_HUB_DB_USER": "hub-user",
        "CSI_HUB_DB_NAME": "hub-database",
        "CSI_HUB_DB_PASSWORD": "hub-password",
        "CSI_HUB_SESSION_SECRET": "hub-session",
        "DOUYIN_ADMIN_HOST": "douyin-host",
        "DOUYIN_ADMIN_PORT": "8300",
        "DOUYIN_DB_HOST": "douyin-db",
        "DOUYIN_DB_PORT": "3309",
        "DOUYIN_DB_USER": "douyin-user",
        "DOUYIN_DB_NAME": "douyin-database",
        "DOUYIN_DB_PASSWORD": "douyin-password",
        "DOUYIN_ADMIN_SESSION_SECRET": "douyin-session",
    }
    for name, value in legacy_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="CSI_OPENBASE_DB_PASSWORD"):
        load_settings(prompt_for_password=False)

    monkeypatch.setenv("CSI_OPENBASE_DB_PASSWORD", "openbase-password")

    settings = load_settings(prompt_for_password=False)

    assert settings.workspace_slug == workspace.slug
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8000
    assert settings.db_host == "127.0.0.1"
    assert settings.db_port == 3306
    assert settings.db_user == "root"
    assert settings.db_name == workspace.database_name
    assert settings.db_password == "openbase-password"
    assert settings.session_secret not in {"hub-session", "douyin-session"}


def test_openbase_database_defaults() -> None:
    assert Settings().db_name == "csi_openbase"
    assert DatabaseConfig().database == "csi_openbase"
