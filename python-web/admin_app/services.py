"""Application services that reuse the established comment pipeline."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .archive_lock import archive_lock, write_collection_state
from .account_data import (
    AccountDataError,
    load_snapshots,
    merge_snapshots,
    normalize_audience_snapshot,
    normalize_profile_snapshot,
    parse_snapshot_bytes,
    write_snapshots,
)
from .config import Settings
from .creator_store import sync_creator_sources
from .work_data import (
    WorkDataError,
    load_work_snapshots,
    merge_work_snapshots,
    read_creator_export,
    write_work_snapshots,
)


from scripts.analyze_comments import main as analyze_comments_main
from scripts.comment_data import (
    CommentDataError,
    load_records,
    merge_record_sets,
    utc_now,
    write_jsonl_atomic,
)
from scripts.import_comments_mysql import (
    ImportDataError,
    connect_mysql,
    create_database_if_missing,
    import_payload,
    load_import_payload,
    validate_progress,
    validate_target_manifest,
    verify_database,
)


VALID_PROGRESS_STATUSES = {"pending", "partial", "complete", "blocked"}
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TARGET_VIDEO_ID_RE = re.compile(r"^[0-9]{8,32}$")
TARGET_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class TargetDataError(ValueError):
    """Raised when a collection-target upload cannot become canonical state."""


def _safe_batch_name(original_name: str | None) -> str:
    supplied = Path(original_name or "comments.jsonl").name
    stem = SAFE_FILENAME_RE.sub("-", Path(supplied).stem).strip("-._") or "comments"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{stem[:80]}-{uuid.uuid4().hex[:8]}.jsonl"


def _safe_creator_export_name(original_name: str | None) -> str:
    supplied = Path(original_name or "creator-export.xlsx").name
    suffix = Path(supplied).suffix.lower()
    if suffix not in {".xlsx", ".csv"}:
        raise WorkDataError("creator export must use the .xlsx or .csv extension")
    stem = SAFE_FILENAME_RE.sub("-", Path(supplied).stem).strip("-._") or "creator-export"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{stem[:80]}-{uuid.uuid4().hex[:8]}{suffix}"


def _target_text(
    value: Any, *, field: str, allow_empty: bool = False, maximum: int | None = None
) -> str:
    if not isinstance(value, str):
        raise TargetDataError(f"{field} must be a string")
    result = value.strip()
    if not allow_empty and not result:
        raise TargetDataError(f"{field} cannot be empty")
    if maximum is not None and len(result) > maximum:
        raise TargetDataError(f"{field} exceeds {maximum} characters")
    return result


def _video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.search(r"(?:^|/)video/([0-9]{8,32})(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    modal_id = parse_qs(parsed.query).get("modal_id", [""])[0]
    return modal_id if TARGET_VIDEO_ID_RE.fullmatch(modal_id) else None


def _validate_target_url(url: str, *, field: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
    ):
        raise TargetDataError(f"{field} must be an HTTPS URL on douyin.com")
    if len(url) > 2048:
        raise TargetDataError(f"{field} exceeds 2048 characters")
    return url


def _normalize_target_upload(
    raw: Any,
    *,
    settings: Settings,
    generated_at: str,
    expected_scope_id: str | None = None,
) -> dict[str, Any]:
    """Expand a compact target list into the established target manifest."""
    if isinstance(raw, list):
        source: dict[str, Any] = {"videos": raw}
    elif isinstance(raw, dict):
        source = raw
    else:
        raise TargetDataError("target file must contain a JSON object or array")
    if source.get("schema_version", 1) != 1:
        raise TargetDataError("target schema_version must be 1")

    videos = source.get("videos")
    if not isinstance(videos, list):
        raise TargetDataError("videos must be an array")

    supplied_collections = source.get("collections")
    if supplied_collections is not None and not isinstance(supplied_collections, list):
        raise TargetDataError("collections must be an array")

    collections: dict[str, dict[str, Any]] = {}
    collection_order: list[str] = []
    for index, item in enumerate(supplied_collections or []):
        if not isinstance(item, dict):
            raise TargetDataError(f"collections[{index}] must be an object")
        collection_id = _target_text(
            item.get("collection_id"),
            field=f"collections[{index}].collection_id",
            maximum=64,
        )
        if not TARGET_COLLECTION_ID_RE.fullmatch(collection_id):
            raise TargetDataError(
                f"collections[{index}].collection_id contains unsupported characters"
            )
        if collection_id in collections:
            raise TargetDataError(f"duplicate collection_id: {collection_id}")
        name = _target_text(
            item.get("name"), field=f"collections[{index}].name", maximum=512
        )
        collections[collection_id] = {
            "collection_id": collection_id,
            "name": name,
        }
        collection_order.append(collection_id)

    normalized_videos: list[dict[str, Any]] = []
    for index, item in enumerate(videos):
        if not isinstance(item, dict):
            raise TargetDataError(f"videos[{index}] must be an object")
        prefix = f"videos[{index}]"
        url = _validate_target_url(
            _target_text(
                item.get("video_url") or item.get("url"),
                field=f"{prefix}.video_url",
            ),
            field=f"{prefix}.video_url",
        )
        supplied_video_id = item.get("video_id")
        if supplied_video_id is None or supplied_video_id == "":
            video_id = _video_id_from_url(url)
            if video_id is None:
                raise TargetDataError(
                    f"{prefix}.video_id is required when the URL does not contain it"
                )
        else:
            video_id = _target_text(supplied_video_id, field=f"{prefix}.video_id")
        if not TARGET_VIDEO_ID_RE.fullmatch(video_id):
            raise TargetDataError(f"{prefix}.video_id must contain 8-32 digits")
        url_video_id = _video_id_from_url(url)
        if url_video_id is not None and url_video_id != video_id:
            raise TargetDataError(
                f"{prefix}.video_id does not match the ID in video_url"
            )

        raw_collection_id = item.get("collection_id")
        if raw_collection_id in (None, ""):
            if len(collections) == 1:
                collection_id = collection_order[0]
            elif not collections:
                collection_id = "all-videos"
            else:
                raise TargetDataError(
                    f"{prefix}.collection_id is required when multiple collections exist"
                )
        else:
            collection_id = _target_text(
                raw_collection_id, field=f"{prefix}.collection_id", maximum=64
            )
        if not TARGET_COLLECTION_ID_RE.fullmatch(collection_id):
            raise TargetDataError(
                f"{prefix}.collection_id contains unsupported characters"
            )
        if collection_id not in collections:
            if supplied_collections is not None:
                raise TargetDataError(
                    f"{prefix}.collection_id references an unknown collection"
                )
            raw_name = item.get("collection_name")
            default_name = "全部作品" if collection_id == "all-videos" else collection_id
            name = _target_text(
                raw_name if raw_name not in (None, "") else default_name,
                field=f"{prefix}.collection_name",
                maximum=512,
            )
            collections[collection_id] = {
                "collection_id": collection_id,
                "name": name,
            }
            collection_order.append(collection_id)

        title_value = item.get("title", "")
        title = _target_text(
            title_value, field=f"{prefix}.title", allow_empty=True
        )
        card_metric = _target_text(
            item.get("card_metric", ""),
            field=f"{prefix}.card_metric",
            allow_empty=True,
            maximum=64,
        )
        normalized_videos.append(
            {
                "video_id": video_id,
                "collection_id": collection_id,
                "collection_name": collections[collection_id]["name"],
                "episode": item.get("episode"),
                "title": title,
                "video_url": url,
                "card_metric": card_metric,
                "status": item.get("status", "pending"),
            }
        )

    by_collection: dict[str, list[dict[str, Any]]] = {}
    for video in normalized_videos:
        by_collection.setdefault(video["collection_id"], []).append(video)
    for collection_id, members in by_collection.items():
        used = {
            item["episode"]
            for item in members
            if isinstance(item.get("episode"), int)
            and not isinstance(item.get("episode"), bool)
            and item["episode"] > 0
        }
        next_episode = 1
        for item in members:
            if item.get("episode") is not None:
                continue
            while next_episode in used:
                next_episode += 1
            item["episode"] = next_episode
            used.add(next_episode)
            next_episode += 1

    canonical_collections = [
        {
            **collections[collection_id],
            "episode_count": len(by_collection.get(collection_id, [])),
        }
        for collection_id in collection_order
    ]
    requested_scope_id = source.get("scope_id")
    if expected_scope_id is not None:
        if requested_scope_id not in (None, ""):
            normalized_requested_scope = _target_text(
                requested_scope_id, field="scope_id", maximum=128
            )
            if normalized_requested_scope != expected_scope_id:
                raise TargetDataError(
                    "scope_id cannot be changed within an existing workspace"
                )
        scope_id = expected_scope_id
    else:
        scope_id = _target_text(
            requested_scope_id or f"{settings.workspace_slug}-default",
            field="scope_id",
            maximum=128,
        )

    manifest = {
        "schema_version": 1,
        "scope_id": scope_id,
        "scope_note": _target_text(
            source.get("scope_note", ""),
            field="scope_note",
            allow_empty=True,
        ),
        "generated_at": generated_at,
        "collections": canonical_collections,
        "target_video_count": len(normalized_videos),
        "completed_video_count": sum(
            item["status"] == "complete" for item in normalized_videos
        ),
        "videos": normalized_videos,
    }
    try:
        return validate_target_manifest(manifest)
    except ImportDataError as exc:
        raise TargetDataError(str(exc)) from exc


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TargetDataError(f"{path.name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TargetDataError(f"{path.name} must contain a JSON object")
    return value


def ingest_target_manifest_bytes(
    content: bytes, original_name: str | None, settings: Settings
) -> dict[str, int]:
    """Validate and atomically replace this workspace's collection targets."""
    if not content:
        raise TargetDataError("uploaded target file is empty")
    if len(content) > settings.max_upload_bytes:
        raise TargetDataError(
            f"uploaded target file exceeds {settings.max_upload_bytes // (1024 * 1024)} MB"
        )
    if original_name and Path(original_name).suffix.lower() != ".json":
        raise TargetDataError("target file must use the .json extension")
    try:
        raw = json.loads(content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise TargetDataError("target file must be UTF-8 encoded") from exc
    except json.JSONDecodeError as exc:
        raise TargetDataError(
            f"target file is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from exc

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    with archive_lock(settings.comments_dir / ".archive.lock"):
        existing_targets = (
            _load_json_object(settings.targets_path)
            if settings.targets_path.exists()
            else {"videos": []}
        )
        existing_progress = (
            _load_json_object(settings.progress_path)
            if settings.progress_path.exists()
            else {"videos": {}}
        )
        if settings.targets_path.exists():
            try:
                validate_target_manifest(existing_targets)
            except ImportDataError as exc:
                raise TargetDataError(f"existing collection state is invalid: {exc}") from exc
        if settings.progress_path.exists():
            if not settings.targets_path.exists():
                raise TargetDataError(
                    "existing collection state has progress but no target manifest"
                )
            try:
                validate_progress(existing_progress, targets=existing_targets)
            except ImportDataError as exc:
                raise TargetDataError(f"existing collection state is invalid: {exc}") from exc
        existing_scope_id = (
            str(existing_targets["scope_id"])
            if settings.targets_path.exists()
            else None
        )
        manifest = _normalize_target_upload(
            raw,
            settings=settings,
            generated_at=now,
            expected_scope_id=existing_scope_id,
        )
        old_target_by_id = {
            item.get("video_id"): item
            for item in existing_targets.get("videos", [])
            if isinstance(item, Mapping) and isinstance(item.get("video_id"), str)
        }
        old_progress_by_id = existing_progress.get("videos", {})
        if not isinstance(old_progress_by_id, dict):
            raise TargetDataError("existing collection-progress.json is invalid")

        try:
            canonical = (
                load_records([settings.canonical_comments_path])
                if settings.canonical_comments_path.exists()
                else []
            )
        except CommentDataError as exc:
            raise TargetDataError(f"existing comments archive is invalid: {exc}") from exc
        comments_by_video: dict[str, list[dict[str, Any]]] = {}
        for record in canonical:
            comments_by_video.setdefault(record["video_id"], []).append(record)

        progress_videos: dict[str, dict[str, Any]] = {}
        preserved_count = 0
        for target in manifest["videos"]:
            video_id = target["video_id"]
            old_progress = old_progress_by_id.get(video_id)
            old_target = old_target_by_id.get(video_id)
            preserved = isinstance(old_progress, Mapping)
            if preserved:
                preserved_count += 1
            previous_status = (
                old_progress.get("status")
                if isinstance(old_progress, Mapping)
                else old_target.get("status") if isinstance(old_target, Mapping) else None
            )
            status = (
                previous_status
                if previous_status in VALID_PROGRESS_STATUSES
                else target["status"]
            )
            target["status"] = status
            matching = comments_by_video.get(video_id, [])
            batches = sorted(
                {
                    row["collection_batch"]
                    for row in matching
                    if row.get("collection_batch")
                }
            )
            latest = max(
                (row["collected_at"] for row in matching), default=None
            )
            progress_videos[video_id] = {
                "title": target["title"],
                "url": target["video_url"],
                "status": status,
                "visible_comment_count": int(
                    old_progress.get("visible_comment_count", 0)
                    if isinstance(old_progress, Mapping)
                    else 0
                ),
                "stored_record_count": len(matching),
                "last_batch": (
                    str(old_progress.get("last_batch") or "")
                    if isinstance(old_progress, Mapping)
                    else batches[-1] if batches else ""
                ),
                "last_collected_at": (
                    old_progress.get("last_collected_at") or latest
                    if isinstance(old_progress, Mapping)
                    else latest
                ),
                "notes": (
                    str(old_progress.get("notes") or "")
                    if isinstance(old_progress, Mapping)
                    else ""
                ),
            }

        completed_count = sum(
            item["status"] == "complete" for item in progress_videos.values()
        )
        manifest["completed_video_count"] = completed_count
        progress = {
            "schema_version": 1,
            "scope_id": manifest["scope_id"],
            "target_manifest": "comments/collection-targets.json",
            "target_video_count": len(manifest["videos"]),
            "completed_video_count": completed_count,
            "coverage_ratio": round(
                completed_count / len(manifest["videos"])
                if manifest["videos"]
                else 0.0,
                4,
            ),
            "stored_record_count": sum(
                item["stored_record_count"] for item in progress_videos.values()
            ),
            "updated_at": now,
            "videos": progress_videos,
        }
        try:
            validate_target_manifest(manifest)
            validate_progress(progress, targets=manifest)
        except ImportDataError as exc:
            raise TargetDataError(str(exc)) from exc
        write_collection_state(
            settings.targets_path, manifest, settings.progress_path, progress
        )

    incoming_ids = {item["video_id"] for item in manifest["videos"]}
    return {
        "target_video_count": len(incoming_ids),
        "collection_count": len(manifest["collections"]),
        "preserved_progress_count": preserved_count,
        "removed_target_count": len(set(old_target_by_id) - incoming_ids),
    }


def _ingest_batch_bytes_unlocked(
    content: bytes, original_name: str | None, settings: Settings
) -> dict[str, Any]:
    """Validate an uploaded JSONL batch, archive it, and merge the canonical store."""
    if not content:
        raise CommentDataError("uploaded batch is empty")
    if len(content) > settings.max_upload_bytes:
        raise CommentDataError(
            f"uploaded batch exceeds {settings.max_upload_bytes // (1024 * 1024)} MB"
        )
    if original_name and Path(original_name).suffix.lower() not in {".jsonl", ".ndjson"}:
        raise CommentDataError("uploaded batch must use the .jsonl or .ndjson extension")

    settings.batches_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.batches_dir / _safe_batch_name(original_name)
    staging = settings.batches_dir / f".{destination.name}.upload"
    try:
        with staging.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        collection_time = utc_now()
        incoming = load_records([staging], default_collected_at=collection_time)
        for record in incoming:
            if not record["collection_batch"]:
                record["collection_batch"] = destination.stem
        existing = (
            load_records(
                [settings.canonical_comments_path],
                default_collected_at=collection_time,
            )
            if settings.canonical_comments_path.exists()
            else []
        )
        merged, stats = merge_record_sets(existing, incoming)
        write_jsonl_atomic(destination, incoming)
        write_jsonl_atomic(settings.canonical_comments_path, merged)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass

    return {
        **stats,
        "batch_path": destination,
        "batch_name": destination.name,
    }


def ingest_batch_bytes(
    content: bytes, original_name: str | None, settings: Settings
) -> dict[str, Any]:
    with archive_lock(settings.comments_dir / ".archive.lock"):
        return _ingest_batch_bytes_unlocked(content, original_name, settings)


def ingest_creator_export_bytes(
    content: bytes, original_name: str | None, settings: Settings
) -> dict[str, Any]:
    """Archive and merge an official creator-center work export."""
    if not content:
        raise WorkDataError("uploaded creator export is empty")
    if len(content) > settings.max_upload_bytes:
        raise WorkDataError(
            f"uploaded creator export exceeds {settings.max_upload_bytes // (1024 * 1024)} MB"
        )
    destination_name = _safe_creator_export_name(original_name)
    imports_dir = settings.works_dir / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    destination = imports_dir / destination_name
    staging = imports_dir / (
        f".{destination.stem}.{uuid.uuid4().hex[:8]}.upload{destination.suffix}"
    )
    lock_path = settings.works_dir / ".archive.lock"
    with archive_lock(lock_path):
        try:
            with staging.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            incoming = read_creator_export(staging, platform=settings.platform)
            incoming = [{**record, "source_file": destination.name} for record in incoming]
            existing = load_work_snapshots(settings.work_snapshots_path)
            merged, stats = merge_work_snapshots(existing, incoming)
            os.replace(staging, destination)
            write_work_snapshots(settings.work_snapshots_path, merged)
        finally:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass
    return {**stats, "source_path": destination, "source_name": destination.name}


def ingest_account_snapshot_bytes(
    content: bytes, *, kind: str, settings: Settings
) -> dict[str, int]:
    if not content:
        raise AccountDataError("uploaded snapshot file is empty")
    if len(content) > settings.max_upload_bytes:
        raise AccountDataError(
            f"uploaded snapshot exceeds {settings.max_upload_bytes // (1024 * 1024)} MB"
        )
    if kind == "profile":
        path = settings.profile_snapshots_path
        normalizer = normalize_profile_snapshot
        identity = ("platform", "observed_at")
    elif kind == "audience":
        path = settings.audience_snapshots_path
        normalizer = normalize_audience_snapshot
        identity = ("platform", "observed_at", "dimension", "segment")
    else:
        raise AccountDataError("snapshot kind must be profile or audience")
    incoming = parse_snapshot_bytes(content, kind=kind, platform=settings.platform)
    with archive_lock(settings.account_dir / ".archive.lock"):
        existing = load_snapshots(path, normalizer)
        merged, duplicates = merge_snapshots(
            existing, incoming, identity_fields=identity
        )
        write_snapshots(path, merged)
    return {
        "incoming_snapshots": len(incoming),
        "stored_snapshots": len(merged),
        "duplicate_snapshots": duplicates,
    }


def _sync_mysql_archive_unlocked(settings: Settings) -> dict[str, Any]:
    """Synchronize canonical files to MySQL with the existing transactional importer."""
    payload = load_import_payload(
        settings.canonical_comments_path,
        settings.targets_path,
        settings.progress_path,
    )
    create_database_if_missing(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    connection = connect_mysql(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    try:
        connection.begin()
        try:
            import_stats, _ = import_payload(
                connection,
                payload,
                comments_path=settings.canonical_comments_path,
                targets_path=settings.targets_path,
                progress_path=settings.progress_path,
                manage_transaction=False,
                verify=False,
            )
            creator_stats = sync_creator_sources(
                connection, settings, manage_transaction=False
            )
            verification = verify_database(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {
        "import": import_stats,
        "creator_data": creator_stats,
        "verification": verification,
    }


def sync_mysql_archive(settings: Settings) -> dict[str, Any]:
    with archive_lock(settings.comments_dir / ".archive.lock"):
        return _sync_mysql_archive_unlocked(settings)


def regenerate_analysis(settings: Settings) -> Path:
    output = settings.reports_dir / "comment-insights.md"
    return_code = analyze_comments_main(
        [
            "--input",
            str(settings.canonical_comments_path),
            "--rules",
            str(settings.theme_rules_path),
            "--output",
            str(output),
            "--workspace-name",
            settings.workspace_name,
            "--platform",
            settings.platform,
        ]
    )
    if return_code != 0:
        raise RuntimeError(f"analysis exited with status {return_code}")
    return output


def _update_collection_progress_unlocked(
    settings: Settings,
    *,
    video_id: str,
    status: str,
    visible_comment_count: int,
    notes: str,
) -> dict[str, Any]:
    """Update the file-backed progress source and matching target manifest atomically."""
    if status not in VALID_PROGRESS_STATUSES:
        raise ValueError("invalid collection status")
    if visible_comment_count < 0:
        raise ValueError("visible comment count cannot be negative")

    progress = json.loads(settings.progress_path.read_text(encoding="utf-8-sig"))
    targets = json.loads(settings.targets_path.read_text(encoding="utf-8-sig"))
    target = next(
        (item for item in targets.get("videos", []) if item.get("video_id") == video_id),
        None,
    )
    if target is None:
        raise ValueError("video is not in the collection target manifest")

    canonical = load_records([settings.canonical_comments_path])
    matching = [row for row in canonical if row["video_id"] == video_id]
    latest = max((row["collected_at"] for row in matching), default=None)
    batches = sorted(
        {row["collection_batch"] for row in matching if row["collection_batch"]}
    )
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

    progress.setdefault("videos", {})[video_id] = {
        "title": target.get("title", ""),
        "url": target.get("video_url", ""),
        "status": status,
        "visible_comment_count": visible_comment_count,
        "stored_record_count": len(matching),
        "last_batch": batches[-1] if batches else "",
        "last_collected_at": latest,
        "notes": notes.strip(),
    }
    progress["completed_video_count"] = sum(
        item.get("status") == "complete" for item in progress["videos"].values()
    )
    progress["stored_record_count"] = sum(
        int(item.get("stored_record_count", 0)) for item in progress["videos"].values()
    )
    target_count = int(progress.get("target_video_count", len(targets["videos"])))
    progress["coverage_ratio"] = round(
        progress["completed_video_count"] / target_count, 4
    )
    progress["updated_at"] = now

    target["status"] = status
    targets["completed_video_count"] = sum(
        item.get("status") == "complete" for item in targets["videos"]
    )
    targets["generated_at"] = now

    write_collection_state(settings.targets_path, targets, settings.progress_path, progress)
    return progress["videos"][video_id]


def update_collection_progress(
    settings: Settings,
    *,
    video_id: str,
    status: str,
    visible_comment_count: int,
    notes: str,
) -> dict[str, Any]:
    with archive_lock(settings.comments_dir / ".archive.lock"):
        return _update_collection_progress_unlocked(
            settings,
            video_id=video_id,
            status=status,
            visible_comment_count=visible_comment_count,
            notes=notes,
        )
