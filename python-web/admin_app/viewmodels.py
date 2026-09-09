"""Presentation helpers for the server-rendered administration UI."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATUS_LABELS = {
    "pending": "待采",
    "partial": "部分完成",
    "complete": "已完成",
    "blocked": "受阻",
    "queued": "等待中",
    "running": "采集中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def local_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SHANGHAI)


def datetime_display(value: datetime | None, *, fallback: str = "—") -> str:
    localized = local_datetime(value)
    return localized.strftime("%Y-%m-%d %H:%M") if localized else fallback


def datetime_iso(value: datetime | None) -> str:
    localized = local_datetime(value)
    return localized.isoformat(timespec="seconds") if localized else ""


def parse_local_datetime(value: str | None) -> datetime | None:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def decorate_video(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    last_collected = result.get("last_collected_at")
    result["last_collected_display"] = datetime_display(
        last_collected, fallback="尚未采集"
    )
    result["last_collected_at"] = datetime_iso(last_collected)
    result["status_label"] = STATUS_LABELS.get(
        str(result.get("status", "pending")), str(result.get("status", "pending"))
    )
    title = str(result.get("title") or result.get("video_id") or "")
    result["short_title"] = title if len(title) <= 48 else f"{title[:47]}…"
    for field in ("comment_count", "root_comment_count", "reply_comment_count", "creator_comment_count"):
        result[field] = int(result.get(field) or 0)
    result["stored_record_count"] = int(
        result.get("stored_record_count") or result.get("comment_count") or 0
    )
    result["visible_comment_count"] = int(result.get("visible_comment_count") or 0)
    return result


def highlight_text(value: str, query: str | None) -> Markup | None:
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    pattern = re.compile(re.escape(cleaned), re.IGNORECASE)
    pieces: list[Markup] = []
    position = 0
    for match in pattern.finditer(value):
        pieces.append(escape(value[position : match.start()]))
        pieces.append(Markup("<mark>"))
        pieces.append(escape(match.group(0)))
        pieces.append(Markup("</mark>"))
        position = match.end()
    if not pieces:
        return None
    pieces.append(escape(value[position:]))
    return Markup("").join(pieces)


def decorate_comment(item: Mapping[str, Any], *, query: str | None = None) -> dict[str, Any]:
    result = dict(item)
    published_at = result.get("published_at")
    collected_at = result.get("last_collected_at")
    result["published_display"] = datetime_display(
        published_at,
        fallback=str(result.get("published_label") or "时间未知"),
    )
    result["published_at"] = datetime_iso(published_at)
    result["last_collected_display"] = datetime_display(collected_at)
    result["last_collected_at"] = datetime_iso(collected_at)
    result["like_count"] = int(result.get("like_count") or 0)
    result["reply_count"] = int(result.get("reply_count") or 0)
    text = str(result.get("text") or "")
    result["text_html"] = highlight_text(text, query)
    return result


def decorate_job(
    item: Mapping[str, Any], *, video_titles: Mapping[str, str] | None = None
) -> dict[str, Any]:
    result = dict(item)
    status = str(result.get("status") or "queued")
    if status == "failed" and result.get("error"):
        result["message"] = str(result["error"])
    result["status_label"] = STATUS_LABELS.get(status, status)
    result["type_label"] = {
        "collect_video": "评论采集",
        "sync_mysql": "数据库同步",
        "analyze": "评论分析",
    }.get(str(result.get("job_type") or ""), str(result.get("job_type") or "任务"))
    video_id = str(result.get("video_id") or "")
    if video_id and video_titles:
        result["video_title"] = video_titles.get(video_id, video_id)
    scheduled = result.get("scheduled_for")
    started = result.get("started_at")
    finished = result.get("finished_at")
    display_time = started or scheduled
    result["scheduled_for_display"] = datetime_display(scheduled)
    result["started_at_display"] = datetime_display(started)
    result["finished_at_display"] = datetime_display(finished, fallback="")
    result["scheduled_for"] = datetime_iso(scheduled)
    result["started_at"] = datetime_iso(started)
    result["finished_at"] = datetime_iso(finished)
    result["time_display"] = datetime_display(display_time)

    progress = {"queued": 0, "running": 50}.get(status, 100)
    payload = result.get("result")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, Mapping):
        capture = payload.get("capture")
        if isinstance(capture, Mapping):
            stored = capture.get("record_count") or capture.get("stored_record_count")
            visible = capture.get("visible_comment_count")
            if stored is not None:
                result["processed_count"] = int(stored)
            if visible is not None:
                result["total_count"] = int(visible)
            if visible and stored is not None:
                progress = min(100, round(int(stored) / int(visible) * 100))
    result["progress_percent"] = progress
    return result


def decorate_import(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["id"] = result.get("import_run_id")
    source = Path(str(result.get("source_path") or ""))
    result["source_name"] = source.name or "未知来源"
    result["short_hash"] = str(result.get("source_sha256") or "")[:12]
    started, completed = result.get("started_at"), result.get("completed_at")
    result["completed_at_display"] = datetime_display(completed)
    result["completed_at"] = datetime_iso(completed)
    if isinstance(started, datetime) and isinstance(completed, datetime):
        seconds = max(0.0, (completed - started).total_seconds())
        result["duration_display"] = f"{seconds:.1f} 秒"
    else:
        result["duration_display"] = "—"
    return result


def pagination(
    *, page: int, total_pages: int, total_items: int, path: str, query: Mapping[str, Any]
) -> dict[str, Any]:
    total_pages = max(1, int(total_pages or 0))
    page = min(max(1, page), total_pages)

    def url_for(target: int) -> str:
        values = {
            key: value
            for key, value in query.items()
            if value not in (None, "") and key != "page"
        }
        values["page"] = target
        return f"{path}?{urlencode(values, doseq=True)}"

    candidates = sorted({1, total_pages, page - 2, page - 1, page, page + 1, page + 2})
    candidates = [value for value in candidates if 1 <= value <= total_pages]
    items: list[dict[str, Any]] = []
    previous = 0
    for value in candidates:
        if previous and value - previous > 1:
            items.append({"ellipsis": True})
        items.append(
            {
                "page": value,
                "label": value,
                "current": value == page,
                "url": url_for(value),
            }
        )
        previous = value
    return {
        "page": page,
        "total_pages": total_pages,
        "total_items": int(total_items),
        "prev_url": url_for(page - 1) if page > 1 else "",
        "next_url": url_for(page + 1) if page < total_pages else "",
        "items": items,
    }


def video_title_map(videos: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(video.get("video_id")): str(video.get("title") or video.get("video_id"))
        for video in videos
    }
