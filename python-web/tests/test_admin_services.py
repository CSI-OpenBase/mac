from __future__ import annotations

import json
from pathlib import Path

import pytest

import admin_app.services as services_module
from admin_app.config import Settings
from admin_app.services import (
    TargetDataError,
    ingest_batch_bytes,
    ingest_target_manifest_bytes,
    update_collection_progress,
)


class TransactionConnection:
    def __init__(self) -> None:
        self.events: list[str] = []

    def begin(self) -> None:
        self.events.append("begin")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def test_uploaded_minimal_batch_is_archived_with_resolved_defaults(tmp_path: Path) -> None:
    settings = Settings(
        repository_root=tmp_path,
        db_password="unused",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    content = json.dumps(
        {"video_id": "7654321098765432109", "text": "低速顿挫正常吗？"},
        ensure_ascii=False,
    ).encode("utf-8")

    result = ingest_batch_bytes(content, "minimal.jsonl", settings)

    archived = [
        json.loads(line)
        for line in result["batch_path"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    canonical = [
        json.loads(line)
        for line in settings.canonical_comments_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    assert len(archived) == 1
    assert archived == canonical
    assert archived[0]["collected_at"].endswith("Z")
    assert archived[0]["collection_batch"] == result["batch_path"].stem
    assert archived[0]["comment_id"].startswith("syn_")


def test_mysql_archive_sync_rolls_back_all_data_when_creator_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        db_password="database-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    connection = TransactionConnection()
    monkeypatch.setattr(services_module, "load_import_payload", lambda *_: object())
    monkeypatch.setattr(
        services_module, "create_database_if_missing", lambda **_: None
    )
    monkeypatch.setattr(services_module, "connect_mysql", lambda **_: connection)

    def import_comments(*args, **kwargs):
        assert args[0] is connection
        assert kwargs["manage_transaction"] is False
        assert kwargs["verify"] is False
        return {"canonical_comment_count": 1}, {}

    def fail_creator_sync(*args, **kwargs):
        assert args == (connection, settings)
        assert kwargs == {"manage_transaction": False}
        raise RuntimeError("creator source failed")

    monkeypatch.setattr(services_module, "import_payload", import_comments)
    monkeypatch.setattr(services_module, "sync_creator_sources", fail_creator_sync)
    monkeypatch.setattr(
        services_module,
        "verify_database",
        lambda *_: pytest.fail("verification must not run after creator sync failed"),
    )

    with pytest.raises(RuntimeError, match="creator source failed"):
        services_module._sync_mysql_archive_unlocked(settings)

    assert connection.events == ["begin", "rollback", "close"]


def test_compact_target_upload_builds_canonical_manifest_and_progress(
    tmp_path: Path,
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        workspace_slug="creator-one",
        workspace_dir=tmp_path / "creator-one",
        db_password="unused",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    first_id = "7654321098765432109"
    second_id = "7654321098765432110"
    content = json.dumps(
        {
            "videos": [
                {
                    "video_url": f"https://www.douyin.com/video/{first_id}",
                    "title": "第一条作品",
                },
                {
                    "video_id": second_id,
                    "video_url": "https://v.douyin.com/compact-link/",
                    "title": "第二条作品",
                },
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")

    result = ingest_target_manifest_bytes(content, "targets.json", settings)

    targets = json.loads(settings.targets_path.read_text(encoding="utf-8"))
    progress = json.loads(settings.progress_path.read_text(encoding="utf-8"))
    assert result == {
        "target_video_count": 2,
        "collection_count": 1,
        "preserved_progress_count": 0,
        "removed_target_count": 0,
    }
    assert targets["scope_id"] == "creator-one-default"
    assert targets["collections"] == [
        {
            "collection_id": "all-videos",
            "name": "全部作品",
            "episode_count": 2,
        }
    ]
    assert [item["video_id"] for item in targets["videos"]] == [first_id, second_id]
    assert [item["episode"] for item in targets["videos"]] == [1, 2]
    assert all(item["status"] == "pending" for item in targets["videos"])
    assert progress["target_video_count"] == 2
    assert progress["completed_video_count"] == 0
    assert set(progress["videos"]) == {first_id, second_id}


def test_target_replacement_preserves_kept_progress_and_comment_history(
    tmp_path: Path,
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        workspace_slug="creator-one",
        workspace_dir=tmp_path / "creator-one",
        db_password="unused",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    kept_id = "7654321098765432109"
    removed_id = "7654321098765432110"
    new_id = "7654321098765432111"

    def target(video_id: str, title: str) -> dict[str, str]:
        return {
            "video_url": f"https://www.douyin.com/video/{video_id}",
            "title": title,
        }

    ingest_target_manifest_bytes(
        json.dumps(
            {"videos": [target(kept_id, "保留作品"), target(removed_id, "移除作品")]},
            ensure_ascii=False,
        ).encode("utf-8"),
        "initial.json",
        settings,
    )
    ingest_batch_bytes(
        json.dumps(
            {"video_id": kept_id, "text": "这是一条保留的评论"},
            ensure_ascii=False,
        ).encode("utf-8"),
        "comments.jsonl",
        settings,
    )
    update_collection_progress(
        settings,
        video_id=kept_id,
        status="complete",
        visible_comment_count=3,
        notes="已完成",
    )

    result = ingest_target_manifest_bytes(
        json.dumps(
            {"videos": [target(kept_id, "更新后的标题"), target(new_id, "新增作品")]},
            ensure_ascii=False,
        ).encode("utf-8"),
        "replacement.json",
        settings,
    )

    targets = json.loads(settings.targets_path.read_text(encoding="utf-8"))
    progress = json.loads(settings.progress_path.read_text(encoding="utf-8"))
    assert result["preserved_progress_count"] == 1
    assert result["removed_target_count"] == 1
    assert {item["video_id"] for item in targets["videos"]} == {kept_id, new_id}
    kept_target = next(item for item in targets["videos"] if item["video_id"] == kept_id)
    assert kept_target["title"] == "更新后的标题"
    assert kept_target["status"] == "complete"
    assert progress["videos"][kept_id]["visible_comment_count"] == 3
    assert progress["videos"][kept_id]["stored_record_count"] == 1
    assert progress["videos"][kept_id]["notes"] == "已完成"
    assert progress["completed_video_count"] == 1
    assert settings.canonical_comments_path.read_text(encoding="utf-8").strip()


def test_existing_workspace_scope_is_inherited_and_cannot_be_replaced(
    tmp_path: Path,
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        workspace_slug="creator-one",
        workspace_dir=tmp_path / "creator-one",
        db_password="unused",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    first_id = "7654321098765432109"
    second_id = "7654321098765432110"
    ingest_target_manifest_bytes(
        json.dumps(
            {
                "scope_id": "stable-workspace-scope",
                "videos": [
                    {
                        "video_url": f"https://www.douyin.com/video/{first_id}",
                        "title": "首个目标",
                    }
                ],
            }
        ).encode("utf-8"),
        "initial.json",
        settings,
    )

    ingest_target_manifest_bytes(
        json.dumps(
            {
                "videos": [
                    {
                        "video_url": f"https://www.douyin.com/video/{second_id}",
                        "title": "替换目标",
                    }
                ]
            }
        ).encode("utf-8"),
        "replacement.json",
        settings,
    )
    targets_before_rejection = settings.targets_path.read_bytes()
    progress_before_rejection = settings.progress_path.read_bytes()
    stored = json.loads(targets_before_rejection.decode("utf-8"))
    assert stored["scope_id"] == "stable-workspace-scope"
    assert [item["video_id"] for item in stored["videos"]] == [second_id]

    with pytest.raises(TargetDataError, match="scope_id cannot be changed"):
        ingest_target_manifest_bytes(
            json.dumps(
                {
                    "scope_id": "different-scope",
                    "videos": [
                        {
                            "video_url": f"https://www.douyin.com/video/{first_id}",
                        }
                    ],
                }
            ).encode("utf-8"),
            "different-scope.json",
            settings,
        )

    assert settings.targets_path.read_bytes() == targets_before_rejection
    assert settings.progress_path.read_bytes() == progress_before_rejection


@pytest.mark.parametrize(
    ("video_id", "video_url", "message"),
    [
        (
            "7654321098765432109",
            "https://example.com/video/7654321098765432109",
            "HTTPS URL on douyin.com",
        ),
        (
            "7654321098765432109",
            "https://www.douyin.com/video/7654321098765432110",
            "does not match the ID in video_url",
        ),
    ],
)
def test_target_upload_rejects_uncollectable_or_mismatched_target_before_writing(
    tmp_path: Path, video_id: str, video_url: str, message: str
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        workspace_dir=tmp_path / "creator-one",
        db_password="unused",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    content = json.dumps(
        {"videos": [{"video_id": video_id, "video_url": video_url}]}
    ).encode("utf-8")

    with pytest.raises(TargetDataError, match=message):
        ingest_target_manifest_bytes(content, "targets.json", settings)

    assert not settings.targets_path.exists()
    assert not settings.progress_path.exists()
