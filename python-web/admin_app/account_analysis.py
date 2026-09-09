"""Transparent baseline analysis across one creator workspace's local data."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .account_data import (
    load_snapshots,
    normalize_audience_snapshot,
    normalize_profile_snapshot,
)
from .config import Settings
from .work_data import load_work_snapshots


NON_PUBLIC_AUDIT_STATUSES = frozenset(
    {
        "私密",
        "仅自己可见",
        "好友可见",
        "审核中",
        "审核未通过",
        "已删除",
        "下架",
        "private",
        "draft",
        "rejected",
        "deleted",
        "unlisted",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _latest_by(
    records: Iterable[Mapping[str, Any]], key: str
) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in records:
        item = dict(raw)
        identity = (str(item.get("platform") or ""), str(item.get(key) or ""))
        current = latest.get(identity)
        if current is None or str(item.get("observed_at") or "") > str(
            current.get("observed_at") or ""
        ):
            latest[identity] = item
    return list(latest.values())


def _load_comments(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    from scripts.comment_data import load_records

    return load_records([path])


def build_account_analysis(settings: Settings) -> dict[str, Any]:
    work_snapshots = load_work_snapshots(settings.work_snapshots_path)
    works = _latest_by(work_snapshots, "work_id")
    public_works = [
        item
        for item in works
        if str(item.get("audit_status") or "").strip().casefold()
        not in NON_PUBLIC_AUDIT_STATUSES
        and int(item.get("view_count") or 0) > 0
    ]
    comments = _load_comments(settings.canonical_comments_path)
    profiles = load_snapshots(
        settings.profile_snapshots_path, normalize_profile_snapshot
    )
    audience = load_snapshots(
        settings.audience_snapshots_path, normalize_audience_snapshot
    )

    total_views = sum(int(item.get("view_count") or 0) for item in public_works)
    total_interactions = sum(
        sum(
            int(item.get(field) or 0)
            for field in ("like_count", "comment_count", "share_count", "collect_count")
        )
        for item in public_works
    )
    total_profile_visits = sum(
        int(item.get("profile_visit_count") or 0) for item in public_works
    )
    total_follower_gain = sum(
        int(item.get("follower_gain") or 0) for item in public_works
    )
    ranked = sorted(
        public_works,
        key=lambda item: (-int(item.get("view_count") or 0), str(item.get("work_id"))),
    )
    root_viewer_comments = [
        item
        for item in comments
        if item.get("comment_type") == "root" and item.get("author_role") == "viewer"
    ]
    creator_replies = [
        item
        for item in comments
        if item.get("comment_type") == "reply" and item.get("author_role") == "creator"
    ]
    questions = [
        item
        for item in root_viewer_comments
        if any(token in str(item.get("text") or "") for token in ("?", "？", "吗", "么", "怎么", "为什么", "为何", "多少", "哪"))
    ]
    latest_profile = max(profiles, key=lambda item: item["observed_at"], default=None)
    earliest_profile = min(profiles, key=lambda item: item["observed_at"], default=None)
    profile_delta = None
    if latest_profile and earliest_profile and latest_profile is not earliest_profile:
        profile_delta = int(latest_profile["follower_count"]) - int(
            earliest_profile["follower_count"]
        )

    latest_audience_time = max(
        (str(item["observed_at"]) for item in audience), default=""
    )
    audience_dimensions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in audience:
        if item["observed_at"] == latest_audience_time:
            audience_dimensions[item["dimension"]].append(item)
    for values in audience_dimensions.values():
        values.sort(key=lambda item: (-float(item["share"]), item["segment"]))

    availability = {
        "profile": bool(profiles),
        "works": bool(work_snapshots),
        "audience": bool(audience),
        "comments": bool(comments),
    }
    signals: list[dict[str, str]] = []
    if public_works:
        top_share = _safe_ratio(
            sum(int(item.get("view_count") or 0) for item in ranked[:3]), total_views
        )
        if top_share is not None and top_share >= 0.6 and len(public_works) >= 5:
            signals.append(
                {
                    "level": "attention",
                    "title": "播放集中在少数作品",
                    "detail": f"播放前三作品占当前样本的 {top_share:.1%}。",
                }
            )
    if root_viewer_comments and questions:
        question_rate = len(questions) / len(root_viewer_comments)
        if question_rate >= 0.25:
            signals.append(
                {
                    "level": "opportunity",
                    "title": "评论中存在明确的问题需求",
                    "detail": f"观众一级评论中，疑问表达占 {question_rate:.1%}。",
                }
            )
    availability_labels = {
        "profile": "账号画像",
        "works": "作品表现",
        "audience": "受众画像",
        "comments": "评论语料",
    }
    missing = [
        availability_labels[key]
        for key, present in availability.items()
        if not present
    ]
    if missing:
        signals.append(
            {
                "level": "data",
                "title": "分析维度尚未完整",
                "detail": "当前缺少：" + "、".join(missing) + "。",
            }
        )

    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "workspace": {
            "slug": settings.workspace_slug,
            "display_name": settings.workspace_name,
            "platform": settings.platform,
        },
        "availability": availability,
        "profile": {
            "snapshot_count": len(profiles),
            "latest": latest_profile,
            "follower_delta": profile_delta,
        },
        "works": {
            "snapshot_count": len(work_snapshots),
            "work_count": len(works),
            "analyzed_work_count": len(public_works),
            "total_views": total_views,
            "median_views": (
                round(statistics.median(int(item["view_count"]) for item in public_works))
                if public_works
                else 0
            ),
            "interaction_rate": _safe_ratio(total_interactions, total_views),
            "profile_visit_rate": _safe_ratio(total_profile_visits, total_views),
            "follower_conversion_rate": _safe_ratio(total_follower_gain, total_views),
            "top_works": [
                {
                    "work_id": item["work_id"],
                    "title": item["title"],
                    "view_count": item["view_count"],
                    "interaction_rate": _safe_ratio(
                        sum(
                            int(item.get(field) or 0)
                            for field in ("like_count", "comment_count", "share_count", "collect_count")
                        ),
                        int(item.get("view_count") or 0),
                    ),
                }
                for item in ranked[:10]
            ],
        },
        "comments": {
            "record_count": len(comments),
            "viewer_root_count": len(root_viewer_comments),
            "question_count": len(questions),
            "question_rate": _safe_ratio(len(questions), len(root_viewer_comments)),
            "creator_reply_count": len(creator_replies),
        },
        "audience": {
            "snapshot_count": len(audience),
            "observed_at": latest_audience_time or None,
            "dimensions": dict(audience_dimensions),
        },
        "signals": signals,
    }


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_account_analysis(settings: Settings, analysis: Mapping[str, Any]) -> Path:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    generated = str(analysis["generated_at"])
    stamp = generated.replace(":", "").replace("-", "")
    versioned = settings.reports_dir / f"account-analysis-{stamp}.json"
    content = json.dumps(dict(analysis), ensure_ascii=False, indent=2) + "\n"
    _write_atomic(versioned, content)
    _write_atomic(settings.reports_dir / "latest.json", content)
    return versioned


def generate_account_analysis(settings: Settings) -> tuple[dict[str, Any], Path]:
    analysis = build_account_analysis(settings)
    return analysis, write_account_analysis(settings, analysis)
