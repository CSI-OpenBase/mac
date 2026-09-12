from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from admin_app.local_store import ActiveCommentJobError, LocalStore


def video_record(video_id: str = "7680023068660346011") -> dict[str, object]:
    return {
        "video_id": video_id,
        "platform": "douyin",
        "title": "归档测试视频",
        "video_url": f"https://www.douyin.com/video/{video_id}",
        "manifest_path": f"works/videos/douyin/{video_id}/manifest.json",
        "first_seen_at": "2026-09-07T10:00:00Z",
        "last_seen_at": "2026-09-07T10:00:00Z",
    }


def video_records(count: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for index in range(count):
        record = video_record(str(7_700_000_000_000_000_000 + index))
        record["title"] = f"分页视频 {index:03d}"
        record["last_seen_at"] = (
            f"2026-09-07T{10 + index // 60:02d}:{index % 60:02d}:00Z"
        )
        records.append(record)
    return records


def test_store_persists_metadata_and_video_index(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    store.set_meta("creator_identity", {"handle": "creator"})
    assert store.get_meta("creator_identity") == {"handle": "creator"}

    assert store.upsert_videos([video_record()]) == 1
    video = store.get_video("7680023068660346011")
    assert video is not None
    assert video["title"] == "归档测试视频"

    updated = video_record()
    updated["title"] = "更新标题"
    updated["last_seen_at"] = "2026-09-08T10:00:00Z"
    store.upsert_videos([updated])
    video = store.get_video("7680023068660346011")
    assert video is not None
    assert video["title"] == "更新标题"
    assert video["first_seen_at"] == "2026-09-07T10:00:00Z"
    assert video["last_seen_at"] == "2026-09-08T10:00:00Z"


def test_video_pages_report_total_and_keep_stable_order(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    records = video_records(65)
    assert store.upsert_videos(records) == 65

    first = store.list_video_page(page=1, page_size=30)
    second = store.list_video_page(page=2, page_size=30)
    last = store.list_video_page(page=99, page_size=30)

    assert first["total"] == 65
    assert first["pages"] == 3
    assert first["page"] == 1
    assert [item["video_id"] for item in first["items"]] == [
        record["video_id"] for record in reversed(records[35:])
    ]
    assert [item["video_id"] for item in second["items"]] == [
        record["video_id"] for record in reversed(records[5:35])
    ]
    assert last["page"] == 3
    assert [item["video_id"] for item in last["items"]] == [
        record["video_id"] for record in reversed(records[:5])
    ]
    assert store.list_videos(limit=100_000) == (
        first["items"] + second["items"] + last["items"]
    )


def test_empty_video_page_stays_on_page_one(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")

    page = store.list_video_page(page=999, page_size=100)

    assert page == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 100,
        "pages": 1,
    }


def test_video_index_records_visible_comments_without_claiming_an_export(
    tmp_path: Path,
) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    initial = video_record()
    initial["visible_comment_count"] = 34
    store.upsert_videos([initial])

    video = store.get_video("7680023068660346011")
    assert video is not None
    assert video["visible_comment_count"] == 34
    assert video["comment_count"] == 0
    assert video["last_comment_export_at"] is None

    missing_metric = video_record()
    missing_metric["last_seen_at"] = "2026-09-08T10:00:00Z"
    store.upsert_videos([missing_metric])
    assert store.get_video("7680023068660346011")["visible_comment_count"] == 34

    refreshed = video_record()
    refreshed["visible_comment_count"] = 21
    refreshed["last_seen_at"] = "2026-09-09T10:00:00Z"
    store.upsert_videos([refreshed])
    assert store.get_video("7680023068660346011")["visible_comment_count"] == 21

    no_comments = video_record()
    no_comments["visible_comment_count"] = 0
    no_comments["last_seen_at"] = "2026-09-10T10:00:00Z"
    store.upsert_videos([no_comments])
    assert store.get_video("7680023068660346011")["visible_comment_count"] == 0


def test_existing_video_index_gains_visible_comment_column(tmp_path: Path) -> None:
    database_path = tmp_path / "openbase.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE archive_videos (
                video_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT 'douyin',
                title TEXT NOT NULL,
                video_url TEXT NOT NULL,
                cover_path TEXT,
                manifest_path TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_comment_export_at TEXT,
                comment_count INTEGER NOT NULL DEFAULT 0,
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO archive_videos(
                video_id, platform, title, video_url, manifest_path,
                first_seen_at, last_seen_at, last_comment_export_at,
                comment_count, record_json, updated_at
            ) VALUES (?, 'douyin', ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                "7680023068660346011",
                "旧档案",
                "https://www.douyin.com/video/7680023068660346011",
                "works/videos/douyin/7680023068660346011/manifest.json",
                "2026-09-07T10:00:00Z",
                "2026-09-07T10:00:00Z",
                "2026-09-07T12:00:00Z",
                27,
                "2026-09-07T12:00:00Z",
            ),
        )

    store = LocalStore(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(archive_videos)")
        }
    assert "visible_comment_count" in columns
    video = store.get_video("7680023068660346011")
    assert video is not None
    assert video["visible_comment_count"] is None
    assert video["comment_count"] == 27
    assert video["last_comment_export_at"] == "2026-09-07T12:00:00Z"


def test_comment_job_is_immediate_and_deduplicated(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    store.upsert_videos([video_record()])
    first = store.create_job("comments", video_id="7680023068660346011")
    assert first["status"] == "queued"
    assert first["payload"] == {}

    with pytest.raises(
        ActiveCommentJobError,
        match="该视频已有等待中或正在运行的评论导出任务",
    ):
        store.create_job("comments", video_id="7680023068660346011")

    store.update_job(first["id"], "succeeded", result={"count": 3})
    second = store.create_job("comments", video_id="7680023068660346011")
    assert second["id"] > first["id"]


@pytest.mark.parametrize("video_id", ["..", "../outside", "1234", "12345678/9"])
def test_store_rejects_video_ids_that_could_escape_paths(
    tmp_path: Path, video_id: str
) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    with pytest.raises(ValueError):
        store.upsert_videos([video_record(video_id)])
    with pytest.raises(ValueError):
        store.create_job("comments", video_id=video_id)


def test_startup_marks_unfinished_work_interrupted_instead_of_resuming(
    tmp_path: Path,
) -> None:
    path = tmp_path / "openbase.sqlite3"
    store = LocalStore(path)
    job = store.create_job("export")
    store.update_job(job["id"], "running")

    restarted = LocalStore(path)
    assert restarted.get_job(job["id"])["status"] == "running"
    assert restarted.interrupt_active_jobs() == 1
    recovered = restarted.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert restarted.active_job_count() == 0


def test_worker_cannot_overwrite_an_interrupted_job(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    job = store.create_job("export")
    store.update_job(job["id"], "running", expected_status="queued")
    store.interrupt_active_jobs()

    from admin_app.local_store import JobStateConflictError

    with pytest.raises(JobStateConflictError):
        store.update_job(
            job["id"], "succeeded", expected_status="running"
        )
    assert store.get_job(job["id"])["status"] == "interrupted"


def test_comment_export_updates_video_summary(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "openbase.sqlite3")
    record = video_record()
    record["visible_comment_count"] = 41
    store.upsert_videos([record])
    store.record_comment_export(
        "7680023068660346011",
        count=27,
        exported_at="2026-09-07T12:00:00Z",
    )
    video = store.get_video("7680023068660346011")
    assert video is not None
    assert video["visible_comment_count"] == 41
    assert video["comment_count"] == 27
    assert video["last_comment_export_at"] == "2026-09-07T12:00:00Z"
