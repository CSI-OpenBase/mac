from __future__ import annotations

from pathlib import Path

import pytest

import admin_app.local_cleanup as cleanup_module
from admin_app.local_cleanup import (
    UnsafeCleanupPathError,
    clear_local_data,
    recover_local_cleanup,
)
from admin_app.local_config import LocalSettings
from admin_app.local_store import ActiveLocalJobsError, LocalStore


VIDEO_ID = "7680023068660346011"
ORPHAN_VIDEO_ID = "7680023068660346022"


def local_settings(tmp_path: Path) -> LocalSettings:
    value = LocalSettings(
        data_home=tmp_path / "workspace",
        session_home=tmp_path / "sessions",
    )
    value.ensure_directories()
    return value


def seed_workspace(tmp_path: Path) -> tuple[LocalSettings, LocalStore]:
    settings = local_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.set_meta("creator_identity", {"handle": "creator-handle"})
    store.set_meta("last_video_sync", {"complete": True})
    store.set_meta("last_export", {"directory": "exports/run-1"})
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "platform": "douyin",
                "title": "归档视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T10:00:00Z",
                "last_seen_at": "2026-09-09T10:00:00Z",
                "visible_comment_count": 41,
            }
        ]
    )
    store.record_comment_export(
        VIDEO_ID,
        count=27,
        exported_at="2026-09-09T12:00:00Z",
    )

    for kind in ("authorize", "sync_videos", "export", "comments"):
        job = store.create_job(
            kind,
            video_id=VIDEO_ID if kind == "comments" else None,
        )
        store.update_job(job["id"], "succeeded")

    (settings.exports_dir / "run-1").mkdir(parents=True)
    (settings.exports_dir / "run-1" / "table.xlsx").write_bytes(b"export")
    video_root = settings.videos_dir / "douyin" / VIDEO_ID
    (video_root / "metadata").mkdir(parents=True)
    (video_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    (video_root / "metadata" / "capture.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (video_root / "comments" / "run-1").mkdir(parents=True)
    (video_root / "comments" / "run-1" / "comments.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    orphan_comments = (
        settings.videos_dir / "douyin" / ORPHAN_VIDEO_ID / "comments" / "run-1"
    )
    orphan_comments.mkdir(parents=True)
    (orphan_comments / "comments.jsonl").write_text("{}\n", encoding="utf-8")
    (settings.discovery_dir / "capture.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (settings.log_dir / "openbase.log").write_text("keep log\n", encoding="utf-8")
    (settings.browser_profile_dir / "Cookies").write_bytes(b"keep authorization")
    (settings.data_home / "user-note.txt").write_text("keep me\n", encoding="utf-8")
    return settings, store


def job_kinds(store: LocalStore) -> set[str]:
    return {str(job["kind"]) for job in store.list_jobs(limit=100)}


def test_clear_exports_removes_only_platform_downloads(tmp_path: Path) -> None:
    settings, store = seed_workspace(tmp_path)

    result = clear_local_data(settings, store, "exports")

    assert result.scope == "exports"
    assert result.files_deleted == 1
    assert settings.exports_dir.is_dir()
    assert list(settings.exports_dir.iterdir()) == []
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert (
        settings.videos_dir
        / "douyin"
        / VIDEO_ID
        / "comments"
        / "run-1"
        / "comments.jsonl"
    ).is_file()
    assert store.get_meta("last_export", {}) == {}
    assert store.get_meta("last_video_sync") == {"complete": True}
    assert store.get_meta("creator_identity") == {"handle": "creator-handle"}
    assert job_kinds(store) == {"authorize", "sync_videos", "comments"}


def test_clear_comments_preserves_video_archive_and_visible_count(
    tmp_path: Path,
) -> None:
    settings, store = seed_workspace(tmp_path)

    result = clear_local_data(settings, store, "comments")

    assert result.scope == "comments"
    assert result.files_deleted == 2
    assert not (settings.videos_dir / "douyin" / VIDEO_ID / "comments").exists()
    assert not (
        settings.videos_dir / "douyin" / ORPHAN_VIDEO_ID / "comments"
    ).exists()
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    video = store.get_video(VIDEO_ID)
    assert video is not None
    assert video["visible_comment_count"] == 41
    assert video["comment_count"] == 0
    assert video["last_comment_export_at"] is None
    assert job_kinds(store) == {"authorize", "sync_videos", "export"}


def test_clear_all_removes_collected_data_but_preserves_workspace_shell(
    tmp_path: Path,
) -> None:
    settings, store = seed_workspace(tmp_path)

    result = clear_local_data(settings, store, "all")

    assert result.scope == "all"
    assert result.files_deleted >= 5
    assert settings.data_home.is_dir()
    assert settings.database_path.is_file()
    assert settings.exports_dir.is_dir()
    assert list(settings.exports_dir.iterdir()) == []
    assert settings.discovery_dir.is_dir()
    assert list(settings.discovery_dir.iterdir()) == []
    assert settings.videos_dir.is_dir()
    assert list(settings.videos_dir.iterdir()) == []
    assert store.list_videos() == []
    assert store.list_jobs() == []
    assert store.get_meta("creator_identity", {}) == {}
    assert (settings.data_home / "user-note.txt").read_text(encoding="utf-8") == "keep me\n"
    assert (settings.log_dir / "openbase.log").is_file()
    assert (settings.browser_profile_dir / "Cookies").is_file()
    assert store.create_job("authorize")["id"] == 1


def test_clear_is_idempotent(tmp_path: Path) -> None:
    settings, store = seed_workspace(tmp_path)

    clear_local_data(settings, store, "comments")
    repeated = clear_local_data(settings, store, "comments")

    assert repeated.files_deleted == 0
    assert repeated.directories_deleted == 0


def test_clear_rejects_active_work_without_mutation(tmp_path: Path) -> None:
    settings, store = seed_workspace(tmp_path)
    store.create_job("authorize")

    with pytest.raises(ActiveLocalJobsError, match="任务"):
        clear_local_data(settings, store, "exports")

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}


def test_clear_rejects_unknown_scope_without_mutation(tmp_path: Path) -> None:
    settings, store = seed_workspace(tmp_path)

    with pytest.raises(ValueError, match="范围"):
        clear_local_data(settings, store, "../outside")

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()


def test_clear_rejects_linked_owned_directory(tmp_path: Path) -> None:
    settings, store = seed_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside\n", encoding="utf-8")
    cleanup_module.shutil.rmtree(settings.exports_dir)
    try:
        settings.exports_dir.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are not available in this environment: {exc}")

    with pytest.raises(UnsafeCleanupPathError):
        clear_local_data(settings, store, "exports")

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "outside\n"
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}


def test_clear_rejects_nested_mount_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)
    simulated_mount = settings.exports_dir / "run-1"
    original_is_mount = Path.is_mount

    def is_mount(path: Path) -> bool:
        return path == simulated_mount or original_is_mount(path)

    monkeypatch.setattr(Path, "is_mount", is_mount)

    with pytest.raises(UnsafeCleanupPathError, match="挂载点"):
        clear_local_data(settings, store, "exports")

    assert (simulated_mount / "table.xlsx").is_file()
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}


def test_staging_failure_does_not_clear_database_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)

    def fail_move(_source: Path, _destination: Path) -> None:
        raise OSError("locked file")

    monkeypatch.setattr(cleanup_module, "_rename_directory", fail_move)

    with pytest.raises(OSError, match="locked file"):
        clear_local_data(settings, store, "exports")

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}
    assert "export" in job_kinds(store)


def test_second_staging_failure_rolls_back_the_first_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)
    original_move = cleanup_module._rename_directory

    def fail_second_move(source: Path, destination: Path) -> None:
        if source == settings.works_dir:
            raise OSError("works is locked")
        original_move(source, destination)

    monkeypatch.setattr(cleanup_module, "_rename_directory", fail_second_move)

    with pytest.raises(OSError, match="works is locked"):
        clear_local_data(settings, store, "all")

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert store.get_meta("creator_identity") == {"handle": "creator-handle"}
    assert store.get_video(VIDEO_ID) is not None


def test_database_failure_restores_staged_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)

    def fail_database(_scope: str, *, operation_id: str) -> None:
        assert operation_id
        raise RuntimeError("database write failed")

    monkeypatch.setattr(store, "clear_records", fail_database)

    with pytest.raises(RuntimeError, match="database write failed"):
        clear_local_data(settings, store, "all")

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert store.get_meta("creator_identity") == {"handle": "creator-handle"}
    assert store.get_video(VIDEO_ID) is not None
    assert not (settings.data_home / ".openbase-trash").exists()


def test_committed_clear_reports_and_retries_pending_physical_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)
    original_delete = cleanup_module.shutil.rmtree

    def fail_staged_delete(path: Path) -> None:
        if path.parent == settings.data_home / ".openbase-trash":
            raise OSError("file remains open")
        original_delete(path)

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", fail_staged_delete)

    result = clear_local_data(settings, store, "exports")

    assert result.pending_directories == 1
    assert list(settings.exports_dir.iterdir()) == []
    assert store.get_meta("last_export", {}) == {}
    assert (settings.data_home / ".openbase-trash").is_dir()

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", original_delete)
    retried = clear_local_data(settings, store, "exports")

    assert retried.pending_directories == 0
    assert not (settings.data_home / ".openbase-trash").exists()


def test_startup_recovery_restores_prepared_clear_after_restart(
    tmp_path: Path,
) -> None:
    settings, store = seed_workspace(tmp_path)
    operation_id = "1" * 32

    with store.exclusive_maintenance():
        operation, _, _ = cleanup_module._stage_directories(
            [settings.exports_dir, settings.works_dir],
            settings.data_home,
            operation_id=operation_id,
            scope="all",
        )

    assert operation is not None
    assert not settings.exports_dir.exists()
    assert not settings.works_dir.exists()

    # Loading settings on a new process recreates this empty directory skeleton.
    settings.ensure_directories()
    pending = recover_local_cleanup(settings, store)

    assert pending == 0
    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert store.get_meta("creator_identity") == {"handle": "creator-handle"}
    assert store.get_video(VIDEO_ID) is not None
    assert store.committed_clear_operations() == {}
    assert not (settings.data_home / ".openbase-trash").exists()


def test_startup_recovery_finishes_committed_clear_after_restart(
    tmp_path: Path,
) -> None:
    settings, store = seed_workspace(tmp_path)
    operation_id = "2" * 32

    with store.exclusive_maintenance():
        operation, _, _ = cleanup_module._stage_directories(
            [settings.exports_dir, settings.works_dir],
            settings.data_home,
            operation_id=operation_id,
            scope="all",
        )
        assert operation is not None
        settings.ensure_directories()
        store.clear_records("all", operation_id=operation_id)

    pending = recover_local_cleanup(settings, store)

    assert pending == 0
    assert list(settings.exports_dir.iterdir()) == []
    assert list(settings.discovery_dir.iterdir()) == []
    assert list(settings.videos_dir.iterdir()) == []
    assert store.list_videos() == []
    assert store.list_jobs() == []
    assert store.get_meta("creator_identity", {}) == {}
    assert store.committed_clear_operations() == {}
    assert not (settings.data_home / ".openbase-trash").exists()


def test_startup_recovery_restores_partially_staged_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, store = seed_workspace(tmp_path)
    original_move = cleanup_module._rename_directory
    moved = 0

    def stop_process_after_first_move(source: Path, destination: Path) -> None:
        nonlocal moved
        original_move(source, destination)
        moved += 1
        if moved == 1:
            raise SystemExit("simulated process termination")

    monkeypatch.setattr(
        cleanup_module, "_rename_directory", stop_process_after_first_move
    )
    with pytest.raises(SystemExit, match="simulated process termination"):
        cleanup_module._stage_directories(
            [settings.exports_dir, settings.works_dir],
            settings.data_home,
            operation_id="3" * 32,
            scope="all",
        )

    monkeypatch.setattr(cleanup_module, "_rename_directory", original_move)
    settings.ensure_directories()
    recover_local_cleanup(settings, store)

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert (settings.videos_dir / "douyin" / VIDEO_ID / "manifest.json").is_file()
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}
    assert not (settings.data_home / ".openbase-trash").exists()


def test_startup_recovery_rejects_tampered_manifest_without_mutation(
    tmp_path: Path,
) -> None:
    settings, store = seed_workspace(tmp_path)
    trash_root = cleanup_module._prepare_trash_root(settings.data_home)
    manifest = trash_root / f"clear-{'4' * 32}.json"
    manifest.write_text(
        """{
  "schema": "csi-openbase.local-clear-operation",
  "version": 1,
  "operation_id": "44444444444444444444444444444444",
  "scope": "exports",
  "targets": [{"original": "../outside", "staged": "0000-outside"}]
}
""",
        encoding="utf-8",
    )

    with pytest.raises(UnsafeCleanupPathError, match="越界"):
        recover_local_cleanup(settings, store)

    assert (settings.exports_dir / "run-1" / "table.xlsx").is_file()
    assert store.get_meta("last_export") == {"directory": "exports/run-1"}
    assert manifest.is_file()


def test_clear_rejects_authorization_nested_in_selected_data(
    tmp_path: Path,
) -> None:
    data_home = tmp_path / "workspace"
    settings = LocalSettings(
        data_home=data_home,
        session_home=data_home / "works" / "browser-session",
    )
    settings.ensure_directories()
    store = LocalStore(settings.database_path)
    store.set_meta("creator_identity", {"handle": "creator-handle"})
    (settings.browser_profile_dir / "Cookies").write_bytes(b"authorization")
    export_file = settings.exports_dir / "run-1" / "table.xlsx"
    export_file.parent.mkdir(parents=True)
    export_file.write_bytes(b"export")

    with pytest.raises(UnsafeCleanupPathError, match="登录授权目录"):
        clear_local_data(settings, store, "all")

    assert (settings.browser_profile_dir / "Cookies").is_file()
    assert export_file.is_file()
    assert store.get_meta("creator_identity") == {"handle": "creator-handle"}
