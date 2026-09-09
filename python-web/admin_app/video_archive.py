"""Discover public Douyin videos and maintain a local, append-only archive.

Only video-level data is materialized.  Author/account objects from Douyin
responses are deliberately ignored so the archive does not become a second
store of unnecessary personal identifiers.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .archive_lock import archive_lock


VIDEO_ARCHIVE_SCHEMA_VERSION = 1
VIDEO_ID_RE = re.compile(r"^[0-9]{8,32}$")
PROFILE_PATH_RE = re.compile(r"^/user/([A-Za-z0-9._~-]{1,256})/?$")
VIDEO_PATH_RE = re.compile(r"(?:^|/)video/([0-9]{8,32})(?:/|$)")
WORK_PATH_RE = re.compile(r"^/(video|note|article)/([^/?#]{1,256})/?$")
PROFILE_HANDLE_RE = re.compile(r"抖音号\s*[：:]\s*([^\s]+)")
PROFILE_WORK_COUNT_RE = re.compile(
    r"(?:^|\s)作品\s*([0-9][0-9,.]*(?:万|亿|[wW])?)"
)
PROFILE_EMPTY_MARKERS = ("暂无作品", "还没有发布过作品", "暂无公开视频")
PROFILE_EMPTY_TEXT_RE = re.compile(
    rf"^\s*(?:{'|'.join(re.escape(marker) for marker in PROFILE_EMPTY_MARKERS)})\s*$"
)
MAX_RESPONSE_NODES = 20_000
MAX_VIDEO_RECORDS = 10_000
MAX_COVER_BYTES = 8 * 1024 * 1024
_PRIVATE_CONTAINER_KEYS = frozenset(
    {
        "author",
        "authors",
        "author_user_id",
        "owner",
        "owners",
        "user",
        "users",
        "followers",
        "following",
    }
)
_COVER_HOST_SUFFIXES = (
    "bytecdn.cn",
    "byteimg.cn",
    "byteimg.com",
    "bytedance.com",
    "douyin.com",
    "douyincdn.com",
    "douyinpic.com",
    "douyinstatic.com",
    "pstatp.com",
    "snssdk.com",
)
_METRIC_ALIASES = {
    "view_count": ("view_count", "play_count", "play", "views"),
    "like_count": ("like_count", "digg_count", "likes", "digg"),
    "comment_count": ("comment_count", "commentCount", "comments"),
    "share_count": ("share_count", "shares"),
    "collect_count": ("collect_count", "favorite_count", "favorites"),
}
_METRIC_LABELS = {
    "view_count": ("播放", "观看"),
    "like_count": ("点赞",),
    "comment_count": ("评论数", "评论"),
    "share_count": ("分享",),
    "collect_count": ("收藏",),
}
_METRIC_VALUE_PATTERN = (
    r"[0-9]+(?:[,，\s][0-9]{3})*(?:\.[0-9]+)?\s*(?:万|亿|[wW])?"
)


class VideoArchiveError(ValueError):
    """Raised when discovery input or an existing archive is unsafe."""


class VideoArchiveIdentityError(VideoArchiveError):
    """Raised before archival when the signed-in profile cannot be bound."""


@dataclass(frozen=True, slots=True)
class ProfileCapture:
    """Sanitized output from one browser capture."""

    records: tuple[dict[str, Any], ...]
    scroll_count: int = 0
    response_count: int = 0
    warnings: tuple[str, ...] = ()
    owner_handle: str = ""
    declared_work_count: int | None = None
    captured_work_count: int | None = None
    listing_complete: bool = True


@dataclass(frozen=True, slots=True)
class VideoArchiveResult:
    """Paths and counters produced by one profile synchronization."""

    profile_url: str
    discovered_at: str
    discovery_dir: Path
    profile_path: Path
    videos_path: Path
    videos: tuple[dict[str, Any], ...]
    created_video_ids: tuple[str, ...]
    updated_video_ids: tuple[str, ...]
    downloaded_covers: tuple[Path, ...]
    warnings: tuple[str, ...]
    declared_work_count: int | None = None
    captured_work_count: int | None = None
    listing_complete: bool = True

    @property
    def discovered_count(self) -> int:
        return len(self.videos)


CaptureFunction = Callable[..., ProfileCapture]
CoverFetcher = Callable[[str], tuple[bytes, str]]


def _douyin_host(hostname: str | None) -> bool:
    host = (hostname or "").rstrip(".").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def _parse_https_url(value: Any, *, field: str) -> Any:
    text = str(value or "").strip()
    if not text or len(text) > 4_096 or "\\" in text:
        raise VideoArchiveError(f"{field} is not a safe HTTPS URL")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise VideoArchiveError(f"{field} is not a safe HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise VideoArchiveError(f"{field} is not a safe HTTPS URL")
    return parsed


def validate_douyin_profile_url(value: str) -> str:
    """Return a canonical Douyin profile URL or reject the input."""

    parsed = _parse_https_url(value, field="profile_url")
    if not _douyin_host(parsed.hostname) or not PROFILE_PATH_RE.fullmatch(parsed.path):
        raise VideoArchiveError(
            "profile_url must be an HTTPS Douyin /user/<id> URL"
        )
    return urlunsplit(
        ("https", parsed.hostname.lower(), parsed.path.rstrip("/"), "", "")
    )


def _profile_handle_from_text(value: str) -> str:
    match = PROFILE_HANDLE_RE.search(value)
    if not match:
        return ""
    return match.group(1).strip("，,;；")[:256]


def _aweme_matches_handle(raw: Mapping[str, Any], expected_handle: str) -> bool:
    author = raw.get("author")
    if not isinstance(author, Mapping):
        return False
    expected = expected_handle.strip().casefold()
    candidates = {
        _clean_text(author.get(key), limit=256).casefold()
        for key in ("unique_id", "uniqueId", "short_id", "shortId")
    }
    candidates.discard("")
    return expected in candidates


def _video_id_from_url(value: Any) -> str | None:
    try:
        parsed = _parse_https_url(value, field="video URL")
    except VideoArchiveError:
        return None
    if not _douyin_host(parsed.hostname):
        return None
    match = VIDEO_PATH_RE.search(parsed.path)
    if match:
        return match.group(1)
    modal_ids = parse_qs(parsed.query, keep_blank_values=False).get("modal_id", [])
    candidate = str(modal_ids[0]) if modal_ids else ""
    return candidate if VIDEO_ID_RE.fullmatch(candidate) else None


def _work_key_from_url(value: Any) -> str | None:
    try:
        parsed = _parse_https_url(value, field="work URL")
    except VideoArchiveError:
        return None
    if not _douyin_host(parsed.hostname):
        return None
    match = WORK_PATH_RE.fullmatch(parsed.path)
    return f"{match.group(1)}:{match.group(2)}" if match else None


def _canonical_video_url(video_id: str) -> str:
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise VideoArchiveError("video_id must contain 8-32 digits")
    return f"https://www.douyin.com/video/{video_id}"


def _clean_text(value: Any, *, limit: int) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _timestamp(value: Any, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise VideoArchiveError("timestamp is required")
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise VideoArchiveError("timestamp is invalid")
        if number > 10_000_000_000:
            number /= 1_000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError) as exc:
            raise VideoArchiveError("timestamp is invalid") from exc
    else:
        text = _clean_text(value, limit=80)
        if text.isdigit():
            return _timestamp(int(text), required=required)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VideoArchiveError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _archive_clock(value: datetime | str | None) -> tuple[str, str]:
    instant = _timestamp(value or datetime.now(timezone.utc), required=True)
    assert instant is not None
    parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    return instant, parsed.strftime("%Y-%m-%d_%H-%M-%S")


def _count(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, str):
        candidate = re.sub(r"\s+", "", value.strip()).replace(",", "").replace("，", "")
        multiplier = 1
        for suffix, scale in (
            ("万", 10_000),
            ("w", 10_000),
            ("W", 10_000),
            ("亿", 100_000_000),
        ):
            if candidate.endswith(suffix):
                multiplier = scale
                candidate = candidate[: -len(suffix)]
                break
    else:
        candidate = value
        multiplier = 1
    try:
        number = float(candidate) * multiplier
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _declared_profile_work_count(value: str) -> int | None:
    match = PROFILE_WORK_COUNT_RE.search(value)
    return _count(match.group(1)) if match else None


def _metrics(raw: Mapping[str, Any]) -> dict[str, int]:
    containers: list[Mapping[str, Any]] = [raw]
    for key in ("statistics", "statistics_v2", "stats", "metrics", "visible_metrics"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            containers.insert(0, value)
    result: dict[str, int] = {}
    for output, aliases in _METRIC_ALIASES.items():
        for container in containers:
            found = False
            for alias in aliases:
                if alias not in container:
                    continue
                normalized = _count(container.get(alias))
                if normalized is not None:
                    result[output] = normalized
                    found = True
                    break
            if found:
                break

    metric_text = _clean_text(raw.get("metric_text"), limit=1_000)
    for output, labels in _METRIC_LABELS.items():
        if output in result:
            continue
        for label in labels:
            patterns = (
                rf"{re.escape(label)}[：:]?\s*({_METRIC_VALUE_PATTERN})",
                rf"({_METRIC_VALUE_PATTERN})\s*{re.escape(label)}",
            )
            match = None
            for pattern in patterns:
                match = re.search(pattern, metric_text)
                if match:
                    break
            if match:
                normalized = _count(match.group(1))
                if normalized is not None:
                    result[output] = normalized
                break
    return result


def _safe_cover_url(value: Any) -> str | None:
    try:
        parsed = _parse_https_url(value, field="cover_url")
    except VideoArchiveError:
        return None
    host = parsed.hostname.rstrip(".").lower()
    if not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _COVER_HOST_SUFFIXES
    ):
        return None
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def _cover_candidates(raw: Mapping[str, Any]) -> Iterable[Any]:
    yield raw.get("cover_url")
    video = raw.get("video")
    if not isinstance(video, Mapping):
        return
    for key in ("cover", "origin_cover", "dynamic_cover"):
        cover = video.get(key)
        if isinstance(cover, Mapping):
            urls = cover.get("url_list")
            if isinstance(urls, list):
                yield from urls[:5]
            yield cover.get("url")
        else:
            yield cover


def _record_from_mapping(
    raw: Mapping[str, Any], *, observed_at: str, source: str
) -> dict[str, Any]:
    supplied_id = raw.get("video_id")
    if supplied_id in (None, ""):
        for key in ("aweme_id", "awemeId", "item_id", "itemId"):
            if raw.get(key) not in (None, ""):
                supplied_id = raw.get(key)
                break
    video_id = _clean_text(supplied_id, limit=40)
    source_url = raw.get("url") or raw.get("video_url") or raw.get("href")
    if not video_id:
        video_id = _video_id_from_url(source_url) or ""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise VideoArchiveError("video_id must contain 8-32 digits")

    description = _clean_text(raw.get("desc") or raw.get("description"), limit=5_000)
    title = _clean_text(
        raw.get("item_title") or raw.get("title") or description,
        limit=500,
    )
    published = None
    for key in ("published_at", "create_time", "createTime", "publish_time"):
        if raw.get(key) not in (None, ""):
            try:
                published = _timestamp(raw.get(key))
            except VideoArchiveError:
                published = None
            break
    cover_url = next(
        (
            safe
            for candidate in _cover_candidates(raw)
            if (safe := _safe_cover_url(candidate))
        ),
        None,
    )
    sources = {source} if source in {"response", "dom"} else set()
    supplied_sources = raw.get("sources")
    if isinstance(supplied_sources, (list, tuple, set)):
        sources.update(
            item for item in supplied_sources if item in {"response", "dom"}
        )
    return {
        "schema_version": VIDEO_ARCHIVE_SCHEMA_VERSION,
        "platform": "douyin",
        "video_id": video_id,
        "url": _canonical_video_url(video_id),
        "title": title,
        "desc": description,
        "published_at": published,
        "cover_url": cover_url,
        "visible_metrics": _metrics(raw),
        "observed_at": observed_at,
        "sources": sorted(sources),
    }


def _looks_like_aweme(value: Mapping[str, Any]) -> bool:
    has_id = any(
        value.get(key) not in (None, "")
        for key in ("aweme_id", "awemeId", "item_id", "itemId", "video_id")
    )
    return has_id and any(
        key in value
        for key in (
            "video",
            "desc",
            "item_title",
            "create_time",
            "statistics",
            "statistics_v2",
        )
    )


def extract_videos_from_response(
    payload: Any,
    *,
    observed_at: datetime | str | None = None,
    expected_handle: str | None = None,
) -> list[dict[str, Any]]:
    """Extract only video-level fields from a decoded Douyin response."""

    observed, _ = _archive_clock(observed_at)
    stack: list[tuple[Any, int]] = [(payload, 0)]
    seen: set[int] = set()
    records: list[dict[str, Any]] = []
    nodes = 0
    while stack and nodes < MAX_RESPONSE_NODES and len(records) < MAX_VIDEO_RECORDS:
        value, depth = stack.pop()
        nodes += 1
        if depth > 12 or not isinstance(value, (Mapping, list, tuple)):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, Mapping):
            if _looks_like_aweme(value) and (
                not expected_handle
                or _aweme_matches_handle(value, expected_handle)
            ):
                try:
                    records.append(
                        _record_from_mapping(
                            value, observed_at=observed, source="response"
                        )
                    )
                except VideoArchiveError:
                    pass
            for key, child in value.items():
                if str(key).casefold() not in _PRIVATE_CONTAINER_KEYS:
                    stack.append((child, depth + 1))
        else:
            stack.extend((child, depth + 1) for child in reversed(value))
    return _merge_records(records)


def extract_videos_from_dom(
    items: Iterable[str | Mapping[str, Any]],
    *,
    observed_at: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Normalize href/title/metric data returned by the small DOM probe."""

    observed, _ = _archive_clock(observed_at)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if index >= MAX_VIDEO_RECORDS:
            break
        raw: Mapping[str, Any] = {"href": item} if isinstance(item, str) else item
        if not isinstance(raw, Mapping):
            continue
        if _video_id_from_url(raw.get("href") or raw.get("url")) is None:
            continue
        try:
            records.append(
                _record_from_mapping(raw, observed_at=observed, source="dom")
            )
        except VideoArchiveError:
            continue
    return _merge_records(records)


def _merge_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    metric_priorities: dict[str, dict[str, int]] = {}
    for raw in records:
        video_id = str(raw["video_id"])
        sources = set(raw.get("sources") or ())
        source_priority = 2 if "response" in sources else 1 if "dom" in sources else 0
        current = merged.get(video_id)
        if current is None:
            merged[video_id] = dict(raw)
            metric_priorities[video_id] = {
                key: source_priority
                for key in dict(raw.get("visible_metrics") or {})
            }
            continue
        for field in ("title", "desc", "published_at", "cover_url"):
            if not current.get(field) and raw.get(field):
                current[field] = raw[field]
        metrics = dict(current.get("visible_metrics") or {})
        priorities = metric_priorities[video_id]
        for key, value in dict(raw.get("visible_metrics") or {}).items():
            priority = priorities.get(key, -1)
            if key not in metrics or source_priority > priority:
                metrics[key] = int(value)
                priorities[key] = source_priority
            elif source_priority == priority:
                metrics[key] = max(metrics[key], int(value))
        current["visible_metrics"] = metrics
        current["sources"] = sorted(
            set(current.get("sources") or ()) | sources
        )
    return [merged[key] for key in sorted(merged, key=lambda item: int(item))]


def _safe_child(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*parts)
    try:
        resolved_target = target.resolve(strict=False)
    except OSError as exc:
        raise VideoArchiveError("archive path cannot be resolved") from exc
    if not resolved_target.is_relative_to(resolved_root):
        raise VideoArchiveError("archive path escapes the works directory")
    return target


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


def _write_jsonl_atomic(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        dict(record), ensure_ascii=False, separators=(",", ":")
                    )
                )
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


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
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


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: Any,
    ) -> None:
        return None


def _fetch_cover(url: str) -> tuple[bytes, str]:
    if _safe_cover_url(url) is None:
        raise VideoArchiveError("cover URL is not on an approved HTTPS host")
    request = Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg",
            "User-Agent": "CSI-OpenBase/1.0",
        },
    )
    try:
        with build_opener(_NoRedirects()).open(request, timeout=15) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_COVER_BYTES:
                raise VideoArchiveError("cover exceeds the size limit")
            content_type = response.headers.get_content_type().lower()
            body = response.read(MAX_COVER_BYTES + 1)
    except HTTPError as exc:
        raise VideoArchiveError(f"cover request failed with HTTP {exc.code}") from exc
    if len(body) > MAX_COVER_BYTES:
        raise VideoArchiveError("cover exceeds the size limit")
    return body, content_type


def _validated_cover(value: bytes, content_type: str) -> tuple[bytes, str]:
    if not isinstance(value, bytes) or not value:
        raise VideoArchiveError("cover response is empty")
    if len(value) > MAX_COVER_BYTES:
        raise VideoArchiveError("cover exceeds the size limit")
    mime = content_type.split(";", 1)[0].strip().lower()
    signatures = (
        ("image/jpeg", ".jpg", value.startswith(b"\xff\xd8\xff")),
        ("image/png", ".png", value.startswith(b"\x89PNG\r\n\x1a\n")),
        (
            "image/webp",
            ".webp",
            len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP",
        ),
    )
    for expected_mime, extension, valid in signatures:
        if valid and mime in ("", expected_mime):
            return value, extension
    raise VideoArchiveError("cover is not a supported raster image")


def _read_manifest(path: Path, *, video_id: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoArchiveError(f"cannot read existing manifest for {video_id}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != VIDEO_ARCHIVE_SCHEMA_VERSION
        or value.get("platform") != "douyin"
        or value.get("video_id") != video_id
    ):
        raise VideoArchiveError(f"existing manifest does not match video {video_id}")
    value["first_seen"] = _timestamp(value.get("first_seen"), required=True)
    value["last_seen"] = _timestamp(value.get("last_seen"), required=True)
    return value


def archive_profile_videos(
    *,
    profile_url: str,
    works_dir: Path,
    records: Iterable[Mapping[str, Any]],
    observed_at: datetime | str | None = None,
    scroll_count: int = 0,
    response_count: int = 0,
    download_covers: bool = False,
    cover_fetcher: CoverFetcher | None = None,
    warnings: Iterable[str] = (),
    declared_work_count: int | None = None,
    captured_work_count: int | None = None,
    listing_complete: bool = True,
) -> VideoArchiveResult:
    """Atomically archive one sanitized profile discovery."""

    canonical_profile = validate_douyin_profile_url(profile_url)
    if scroll_count < 0 or response_count < 0:
        raise VideoArchiveError("capture counters cannot be negative")
    if declared_work_count is not None and declared_work_count < 0:
        raise VideoArchiveError("declared work count cannot be negative")
    if captured_work_count is not None and captured_work_count < 0:
        raise VideoArchiveError("captured work count cannot be negative")
    discovered_at, timestamp_name = _archive_clock(observed_at)
    works = works_dir.expanduser().resolve()
    works.mkdir(parents=True, exist_ok=True)

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        if index >= MAX_VIDEO_RECORDS:
            raise VideoArchiveError(
                f"profile discovery exceeds the {MAX_VIDEO_RECORDS:,}-video limit"
            )
        if not isinstance(raw, Mapping):
            raise VideoArchiveError("video record must be an object")
        normalized.append(
            _record_from_mapping(
                raw,
                observed_at=discovered_at,
                source="",
            )
        )
    videos = _merge_records(normalized)
    warning_list = []
    for item in warnings:
        warning = _clean_text(item, limit=500)
        if warning:
            warning_list.append(warning)

    downloaded: dict[str, Path] = {}
    fetch = cover_fetcher or _fetch_cover
    if download_covers:
        for record in videos:
            cover_url = record.get("cover_url")
            if not cover_url:
                continue
            video_id = record["video_id"]
            try:
                body, content_type = fetch(str(cover_url))
                body, extension = _validated_cover(body, content_type)
                path = _safe_child(
                    works, "videos", "douyin", video_id, f"cover{extension}"
                )
                _write_bytes_atomic(path, body)
                downloaded[video_id] = path
            except Exception as exc:
                warning_list.append(
                    f"video {video_id}: cover download failed ({type(exc).__name__})"
                )

    discovery_dir = _safe_child(works, "discovery", timestamp_name)
    profile_path = _safe_child(
        works, "discovery", timestamp_name, "profile.json"
    )
    videos_path = _safe_child(
        works, "discovery", timestamp_name, "videos.jsonl"
    )
    created_ids: list[str] = []
    updated_ids: list[str] = []

    with archive_lock(_safe_child(works, ".video-archive.lock")):
        plans: list[tuple[dict[str, Any], Path, Path, dict[str, Any]]] = []
        for record in videos:
            video_id = record["video_id"]
            manifest_path = _safe_child(
                works, "videos", "douyin", video_id, "manifest.json"
            )
            metadata_path = _safe_child(
                works,
                "videos",
                "douyin",
                video_id,
                "metadata",
                f"{timestamp_name}.json",
            )
            existing = _read_manifest(manifest_path, video_id=video_id)
            if existing is None:
                first_seen = discovered_at
                last_seen = discovered_at
                latest_metadata = f"metadata/{timestamp_name}.json"
                created_ids.append(video_id)
            else:
                first_seen = min(str(existing["first_seen"]), discovered_at)
                last_seen = max(str(existing["last_seen"]), discovered_at)
                latest_metadata = (
                    f"metadata/{timestamp_name}.json"
                    if discovered_at >= str(existing["last_seen"])
                    else str(existing.get("latest_metadata") or "")
                )
                updated_ids.append(video_id)
            current_is_latest = (
                existing is None or discovered_at >= str(existing["last_seen"])
            )
            cover = existing.get("cover") if existing else None
            if video_id in downloaded:
                cover = {
                    "url": record.get("cover_url"),
                    "path": downloaded[video_id].name,
                }
            existing_title = str(existing.get("title") or "") if existing else ""
            observed_title = str(record.get("title") or "")
            manifest = {
                "schema_version": VIDEO_ARCHIVE_SCHEMA_VERSION,
                "platform": "douyin",
                "video_id": video_id,
                "url": record["url"],
                "title": (
                    observed_title or existing_title
                    if current_is_latest
                    else existing_title
                ),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "latest_metadata": latest_metadata,
                "cover": cover,
            }
            plans.append((record, metadata_path, manifest_path, manifest))

        for record, metadata_path, manifest_path, manifest in plans:
            _write_json_atomic(metadata_path, record)
            _write_json_atomic(manifest_path, manifest)

        _write_json_atomic(
            profile_path,
            {
                "schema_version": VIDEO_ARCHIVE_SCHEMA_VERSION,
                "platform": "douyin",
                "profile_url": canonical_profile,
                "discovered_at": discovered_at,
                "video_count": len(videos),
                "scroll_count": scroll_count,
                "response_count": response_count,
                "declared_work_count": declared_work_count,
                "captured_work_count": captured_work_count,
                "listing_complete": listing_complete,
            },
        )
        _write_jsonl_atomic(videos_path, videos)

    return VideoArchiveResult(
        profile_url=canonical_profile,
        discovered_at=discovered_at,
        discovery_dir=discovery_dir,
        profile_path=profile_path,
        videos_path=videos_path,
        videos=tuple(videos),
        created_video_ids=tuple(created_ids),
        updated_video_ids=tuple(updated_ids),
        downloaded_covers=tuple(downloaded[key] for key in sorted(downloaded)),
        warnings=tuple(warning_list),
        declared_work_count=declared_work_count,
        captured_work_count=captured_work_count,
        listing_complete=listing_complete,
    )


def _launch_persistent_context(playwright: Any, profile: Path) -> Any:
    options = {
        "headless": False,
        "locale": "zh-CN",
        "viewport": {"width": 1440, "height": 960},
    }
    failures: list[str] = []
    for channel in (None, "chrome", "msedge"):
        try:
            if channel is None:
                return playwright.chromium.launch_persistent_context(
                    str(profile), **options
                )
            return playwright.chromium.launch_persistent_context(
                str(profile), channel=channel, **options
            )
        except Exception as exc:
            failures.append(f"{channel or 'playwright'}: {type(exc).__name__}")
    raise VideoArchiveError(
        "no compatible Chromium browser could be launched ("
        + ", ".join(failures)
        + ")"
    )


_DOM_PROBE = r"""
(anchors) => anchors.map((anchor) => {
  const image = anchor.querySelector('img');
  const paragraphTexts = Array.from(anchor.querySelectorAll('p'))
    .map((node) => (node.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  const description = paragraphTexts.find((text) =>
    !/^[0-9.,]+(?:万|亿|[wW])?$/.test(text)
  ) || ((image && image.alt) || '').replace(/^[^：:]{1,100}[：:]\s*/, '');
  const metricNodes = anchor.querySelectorAll(
    '[data-e2e*="like"], [data-e2e*="play"], [data-e2e*="comment"], ' +
    '[aria-label*="点赞"], ' +
    '[aria-label*="播放"], [aria-label*="评论"], [aria-label*="收藏"]'
  );
  const visiblePlayCounts = Array.from(anchor.querySelectorAll('span'))
    .map((node) => (node.textContent || '').replace(/\s+/g, '').trim())
    .filter((text) => /^[0-9.,]+(?:万|亿|[wW])?$/.test(text))
    .map((text) => `播放 ${text}`);
  return {
    href: anchor.href || '',
    title: anchor.getAttribute('title') || description,
    desc: description,
    metric_text: Array.from(metricNodes).map((node) =>
      node.getAttribute('aria-label') || node.textContent || ''
    ).concat(visiblePlayCounts).join(' ')
  };
})
"""

_WORK_LINK_SELECTOR = ", ".join(
    f'[data-e2e="user-post-list"] a[href*="/{kind}/"]'
    for kind in ("video", "note", "article")
)
_WORK_HREF_PROBE = "(anchors) => anchors.map((anchor) => anchor.href || '')"


def _profile_has_explicit_empty_state(page: Any) -> bool:
    """Accept empty-copy only inside the profile list's semantic empty state."""

    try:
        matches = page.get_by_text(PROFILE_EMPTY_TEXT_RE)
        count = min(matches.count(), 10)
    except Exception:
        return False
    boundary_selector = (
        "xpath=ancestor-or-self::*["
        "@role='status' or @role='alert' or "
        "@data-e2e='user-post-list' or contains(@data-e2e, 'empty')]"
    )
    for index in range(count):
        marker = matches.nth(index)
        try:
            if not marker.is_visible():
                continue
            boundaries = marker.locator(boundary_selector)
            for boundary_index in range(min(boundaries.count(), 10)):
                if boundaries.nth(boundary_index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _profile_listing_complete(
    declared_count: int | None,
    captured_count: int,
    *,
    explicit_empty: bool,
) -> bool:
    if declared_count is not None:
        return captured_count >= declared_count
    return captured_count == 0 and explicit_empty


def _relevant_response_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    path = parsed.path.casefold()
    return _douyin_host(parsed.hostname) and "aweme" in path and any(
        token in path for token in ("post", "detail", "profile")
    )


def _capture_with_playwright(
    *,
    profile_url: str,
    browser_profile_dir: Path,
    observed_at: str,
    max_scrolls: int,
    stable_rounds: int,
    scroll_pause_ms: int,
    expected_handle: str | None = None,
) -> ProfileCapture:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - installation failure
        raise VideoArchiveError("Playwright and Chromium are required") from exc

    profile = browser_profile_dir.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        profile.chmod(0o700)
    except OSError:
        pass

    records: list[dict[str, Any]] = []
    work_keys: set[str] = set()
    warnings: list[str] = []
    response_count = 0
    scroll_count = 0
    owner_handle = ""
    declared_count: int | None = None
    explicit_empty = False
    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, profile)
        try:
            def handle_response(response: Any) -> None:
                nonlocal response_count
                if not _relevant_response_url(str(response.url)):
                    return
                try:
                    payload = response.json()
                    response_count += 1
                    records.extend(
                        extract_videos_from_response(
                            payload,
                            observed_at=observed_at,
                            expected_handle=expected_handle,
                        )
                    )
                except Exception as exc:
                    warnings.append(
                        "a relevant profile response could not be parsed "
                        f"({type(exc).__name__})"
                    )

            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", handle_response)
            page.goto(profile_url, wait_until="domcontentloaded", timeout=45_000)

            def inspect_dom(*, record_warnings: bool) -> None:
                try:
                    dom_items = page.locator(
                        '[data-e2e="user-post-list"] a[href*="/video/"]'
                    ).evaluate_all(_DOM_PROBE)
                    records.extend(
                        extract_videos_from_dom(
                            dom_items, observed_at=observed_at
                        )
                    )
                except Exception as exc:
                    if record_warnings:
                        warnings.append(
                            f"profile DOM could not be inspected ({type(exc).__name__})"
                        )
                try:
                    work_hrefs = page.locator(_WORK_LINK_SELECTOR).evaluate_all(
                        _WORK_HREF_PROBE
                    )
                    work_keys.update(
                        key
                        for href in work_hrefs
                        if (key := _work_key_from_url(href)) is not None
                    )
                except Exception as exc:
                    if record_warnings:
                        warnings.append(
                            "profile works could not be counted "
                            f"({type(exc).__name__})"
                        )

            # A successful DOMContentLoaded still shows only a loading shell on
            # Douyin for several seconds. Require both the signed-in profile
            # header and a conclusive works-list state before scrolling.
            listing_ready = False
            body_text = ""
            for readiness_round in range(41):
                inspect_dom(record_warnings=False)
                try:
                    body_text = page.locator("body").inner_text(timeout=5_000)
                    owner_handle = _profile_handle_from_text(body_text)
                except Exception:
                    body_text = ""
                declared_count = _declared_profile_work_count(body_text)
                explicit_empty = declared_count == 0 or (
                    declared_count is None
                    and not work_keys
                    and _profile_has_explicit_empty_state(page)
                )
                listing_ready = bool(work_keys) or explicit_empty
                identity_ready = not expected_handle or bool(owner_handle)
                if listing_ready and identity_ready:
                    break
                if readiness_round < 40:
                    page.wait_for_timeout(500)
            if not listing_ready:
                raise VideoArchiveError(
                    "the Douyin profile works list did not finish loading"
                )
            if expected_handle and not owner_handle:
                raise VideoArchiveIdentityError(
                    "the signed-in profile identity could not be verified"
                )

            previous_work_keys: set[str] = set()
            unchanged_rounds = 0
            for index in range(max_scrolls + 1 if work_keys else 0):
                inspect_dom(record_warnings=True)
                unchanged_rounds = (
                    unchanged_rounds + 1
                    if work_keys == previous_work_keys
                    else 0
                )
                previous_work_keys = set(work_keys)
                reached_declared_count = (
                    declared_count is not None
                    and len(work_keys) >= declared_count
                )
                if (
                    reached_declared_count
                    or (
                        declared_count is None
                        and unchanged_rounds >= stable_rounds
                    )
                    or index >= max_scrolls
                ):
                    break
                try:
                    viewport_height = int(
                        page.evaluate("() => window.innerHeight") or 960
                    )
                    page.mouse.move(720, max(100, viewport_height - 160))
                    page.mouse.wheel(0, max(400, int(viewport_height * 0.75)))
                    scroll_count += 1
                    page.wait_for_timeout(scroll_pause_ms)
                except Exception as exc:
                    warnings.append(
                        f"profile scrolling stopped ({type(exc).__name__})"
                    )
                    break
            current_url = str(getattr(page, "url", "")).casefold()
            if any(token in current_url for token in ("passport", "captcha", "verify")):
                warnings.append("Douyin login or verification is required")
            try:
                body_text = page.locator("body").inner_text(timeout=5_000)
                owner_handle = _profile_handle_from_text(body_text)
                final_declared_count = _declared_profile_work_count(body_text)
                if final_declared_count is not None:
                    declared_count = final_declared_count
            except Exception as exc:
                warnings.append(
                    f"profile owner could not be inspected ({type(exc).__name__})"
                )
        finally:
            context.close()
    dom_video_ids = {
        key.split(":", 1)[1]
        for key in work_keys
        if key.startswith("video:")
    }
    merged_records = tuple(
        record
        for record in _merge_records(records)
        if record["video_id"] in dom_video_ids
    )
    listing_complete = _profile_listing_complete(
        declared_count,
        len(work_keys),
        explicit_empty=explicit_empty,
    )
    if not listing_complete:
        declared_label = (
            str(declared_count) if declared_count is not None else "unknown"
        )
        warnings.append(
            "profile works capture was incomplete "
            f"(declared={declared_label}, captured={len(work_keys)})"
        )
    return ProfileCapture(
        records=merged_records,
        scroll_count=scroll_count,
        response_count=response_count,
        warnings=tuple(dict.fromkeys(warnings)),
        owner_handle=owner_handle,
        declared_work_count=declared_count,
        captured_work_count=len(work_keys),
        listing_complete=listing_complete,
    )


def sync_profile_videos(
    *,
    profile_url: str,
    works_dir: Path,
    browser_profile_dir: Path | None = None,
    observed_at: datetime | str | None = None,
    max_scrolls: int = 80,
    stable_rounds: int = 4,
    scroll_pause_ms: int = 1_000,
    download_covers: bool = False,
    capture: CaptureFunction | None = None,
    cover_fetcher: CoverFetcher | None = None,
    expected_handle: str | None = None,
) -> VideoArchiveResult:
    """Capture a Douyin profile with a headed browser and archive the result.

    ``capture`` is injectable so parsing and archival can be exercised without
    launching a browser in unit tests.
    """

    canonical_profile = validate_douyin_profile_url(profile_url)
    if not 1 <= max_scrolls <= 500:
        raise VideoArchiveError("max_scrolls must be between 1 and 500")
    if not 1 <= stable_rounds <= 20:
        raise VideoArchiveError("stable_rounds must be between 1 and 20")
    if not 100 <= scroll_pause_ms <= 10_000:
        raise VideoArchiveError("scroll_pause_ms must be between 100 and 10000")
    discovered_at, _ = _archive_clock(observed_at)
    capture_function = capture or _capture_with_playwright
    if capture is None and browser_profile_dir is None:
        raise VideoArchiveError(
            "browser_profile_dir is required for Playwright profile discovery"
        )
    profile_dir = browser_profile_dir or works_dir / ".browser-profile"
    captured = capture_function(
        profile_url=canonical_profile,
        browser_profile_dir=profile_dir,
        observed_at=discovered_at,
        max_scrolls=max_scrolls,
        stable_rounds=stable_rounds,
        scroll_pause_ms=scroll_pause_ms,
        expected_handle=str(expected_handle or "").strip() or None,
    )
    if not isinstance(captured, ProfileCapture):
        raise VideoArchiveError("capture must return ProfileCapture")
    expected = str(expected_handle or "").strip()
    actual = str(captured.owner_handle or "").strip()
    if expected and not actual:
        raise VideoArchiveIdentityError(
            "无法确认个人主页账号，请在打开的浏览器中登录后重试"
        )
    if expected and actual.casefold() != expected.casefold():
        raise VideoArchiveIdentityError(
            "个人主页账号与当前工作目录授权的创作者不一致"
        )
    return archive_profile_videos(
        profile_url=canonical_profile,
        works_dir=works_dir,
        records=captured.records,
        observed_at=discovered_at,
        scroll_count=captured.scroll_count,
        response_count=captured.response_count,
        download_covers=download_covers,
        cover_fetcher=cover_fetcher,
        warnings=captured.warnings,
        declared_work_count=captured.declared_work_count,
        captured_work_count=captured.captured_work_count,
        listing_complete=captured.listing_complete,
    )
