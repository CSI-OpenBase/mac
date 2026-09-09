from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from admin_app.account_analysis import build_account_analysis
from admin_app.config import Settings
from admin_app.work_data import normalize_work_snapshot, write_work_snapshots
from scripts.comment_data import normalize_record, write_jsonl_atomic


def _write_jsonl(path: Path, records: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def test_account_analysis_combines_works_comments_profile_and_audience(
    tmp_path: Path,
) -> None:
    settings = Settings(
        workspace_slug="creator-a",
        workspace_name="创作者 A",
        platform="douyin",
        workspace_dir=tmp_path,
        db_name="csi_openbase_creator_a",
    )
    common_work = {
        "platform": "douyin",
        "published_at": "2026-09-01T00:00:00Z",
        "audit_status": "公开",
    }
    work_snapshots = [
        normalize_work_snapshot(
            {
                **common_work,
                "work_id": "work-1",
                "title": "低速跟车",
                "observed_at": "2026-09-05T00:00:00Z",
                "view_count": 800,
                "like_count": 80,
            }
        ),
        normalize_work_snapshot(
            {
                **common_work,
                "work_id": "work-1",
                "title": "低速跟车",
                "observed_at": "2026-09-06T00:00:00Z",
                "view_count": 1_000,
                "like_count": 100,
                "comment_count": 30,
                "share_count": 20,
                "collect_count": 50,
                "profile_visit_count": 100,
                "follower_gain": 20,
            }
        ),
        normalize_work_snapshot(
            {
                **common_work,
                "work_id": "work-2",
                "title": "坡道驾驶",
                "observed_at": "2026-09-06T00:00:00Z",
                "view_count": 500,
                "like_count": 50,
                "comment_count": 10,
                "share_count": 5,
                "collect_count": 10,
                "profile_visit_count": 25,
                "follower_gain": 5,
            }
        ),
        normalize_work_snapshot(
            {
                **common_work,
                "work_id": "work-private",
                "title": "未公开作品",
                "audit_status": "仅自己可见",
                "observed_at": "2026-09-06T00:00:00Z",
                "view_count": 9_000,
            }
        ),
    ]
    write_work_snapshots(settings.work_snapshots_path, work_snapshots)

    root_question = normalize_record(
        {
            "video_id": "work-1",
            "comment_id": "comment-1",
            "comment_type": "root",
            "author_role": "viewer",
            "text": "堵车时应该怎么开？",
            "reply_count": 1,
            "collected_at": "2026-09-06T01:00:00Z",
        }
    )
    creator_reply = normalize_record(
        {
            "video_id": "work-1",
            "comment_id": "reply-1",
            "comment_type": "reply",
            "parent_comment_id": "comment-1",
            "root_comment_id": "comment-1",
            "author_role": "creator",
            "text": "建议保持车距。",
            "collected_at": "2026-09-06T01:00:00Z",
        }
    )
    root_statement = normalize_record(
        {
            "video_id": "work-2",
            "comment_id": "comment-2",
            "comment_type": "root",
            "author_role": "viewer",
            "text": "这个方法很实用。",
            "collected_at": "2026-09-06T01:00:00Z",
        }
    )
    write_jsonl_atomic(
        settings.canonical_comments_path,
        [root_question, creator_reply, root_statement],
    )

    _write_jsonl(
        settings.profile_snapshots_path,
        [
            {
                "platform": "douyin",
                "observed_at": "2026-09-05T00:00:00Z",
                "display_name": "创作者 A",
                "follower_count": 100,
                "following_count": 10,
                "total_like_count": 1_000,
                "work_count": 2,
            },
            {
                "platform": "douyin",
                "observed_at": "2026-09-06T00:00:00Z",
                "display_name": "创作者 A",
                "follower_count": 135,
                "following_count": 11,
                "total_like_count": 1_300,
                "work_count": 3,
            },
        ],
    )
    _write_jsonl(
        settings.audience_snapshots_path,
        [
            {
                "platform": "douyin",
                "observed_at": "2026-09-05T00:00:00Z",
                "dimension": "gender",
                "segment": "female",
                "share": 0.4,
            },
            {
                "platform": "douyin",
                "observed_at": "2026-09-06T00:00:00Z",
                "dimension": "gender",
                "segment": "male",
                "share": 0.7,
            },
            {
                "platform": "douyin",
                "observed_at": "2026-09-06T00:00:00Z",
                "dimension": "gender",
                "segment": "female",
                "share": 0.3,
            },
            {
                "platform": "douyin",
                "observed_at": "2026-09-06T00:00:00Z",
                "dimension": "age",
                "segment": "25-34",
                "share": 0.6,
            },
        ],
    )

    analysis = build_account_analysis(settings)

    assert analysis["workspace"] == {
        "slug": "creator-a",
        "display_name": "创作者 A",
        "platform": "douyin",
    }
    assert analysis["availability"] == {
        "profile": True,
        "works": True,
        "audience": True,
        "comments": True,
    }
    assert analysis["works"]["snapshot_count"] == 4
    assert analysis["works"]["work_count"] == 3
    assert analysis["works"]["analyzed_work_count"] == 2
    assert analysis["works"]["total_views"] == 1_500
    assert analysis["works"]["median_views"] == 750
    assert analysis["works"]["interaction_rate"] == 0.183333
    assert analysis["works"]["profile_visit_rate"] == 0.083333
    assert analysis["works"]["follower_conversion_rate"] == 0.016667
    assert analysis["works"]["top_works"][0]["work_id"] == "work-1"
    assert analysis["comments"] == {
        "record_count": 3,
        "viewer_root_count": 2,
        "question_count": 1,
        "question_rate": 0.5,
        "creator_reply_count": 1,
    }
    assert analysis["profile"]["snapshot_count"] == 2
    assert analysis["profile"]["follower_delta"] == 35
    assert analysis["audience"]["snapshot_count"] == 4
    assert analysis["audience"]["observed_at"] == "2026-09-06T00:00:00Z"
    assert [
        item["segment"] for item in analysis["audience"]["dimensions"]["gender"]
    ] == ["male", "female"]
    assert any(signal["level"] == "opportunity" for signal in analysis["signals"])


def test_account_analysis_uses_human_labels_for_missing_sources(tmp_path: Path) -> None:
    settings = Settings(
        workspace_slug="empty-creator",
        workspace_name="空工作区",
        platform="douyin",
        workspace_dir=tmp_path,
        db_name="csi_openbase_empty_creator",
    )

    analysis = build_account_analysis(settings)

    assert analysis["signals"] == [
        {
            "level": "data",
            "title": "分析维度尚未完整",
            "detail": "当前缺少：账号画像、作品表现、受众画像、评论语料。",
        }
    ]


def test_account_analysis_includes_published_status_variants(tmp_path: Path) -> None:
    settings = Settings(
        workspace_slug="creator-a",
        workspace_name="创作者 A",
        platform="douyin",
        workspace_dir=tmp_path,
        db_name="csi_openbase_creator_a",
    )
    snapshots = [
        normalize_work_snapshot(
            {
                "work_id": "published-work",
                "title": "已经发布",
                "audit_status": "审核通过",
                "observed_at": "2026-09-06T00:00:00Z",
                "view_count": 300,
            }
        ),
        normalize_work_snapshot(
            {
                "work_id": "draft-work",
                "title": "仍在审核",
                "audit_status": "审核中",
                "observed_at": "2026-09-06T00:00:00Z",
                "view_count": 900,
            }
        ),
    ]
    write_work_snapshots(settings.work_snapshots_path, snapshots)

    analysis = build_account_analysis(settings)

    assert analysis["works"]["analyzed_work_count"] == 1
    assert analysis["works"]["total_views"] == 300
    assert analysis["works"]["top_works"][0]["work_id"] == "published-work"
