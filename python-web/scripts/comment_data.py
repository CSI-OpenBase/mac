#!/usr/bin/env python3
"""Shared validation and merge helpers for creator comment archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 1
PLATFORM = "douyin"

CANONICAL_FIELDS = (
    "schema_version",
    "platform",
    "comment_id",
    "comment_id_kind",
    "video_id",
    "video_title",
    "video_url",
    "parent_comment_id",
    "root_comment_id",
    "comment_type",
    "author_role",
    "text",
    "like_count",
    "reply_count",
    "published_at",
    "published_label",
    "collected_at",
    "topic_ids",
    "manual_tags",
    "source_url",
    "collection_batch",
)

DISALLOWED_PERSONAL_FIELDS = {
    "author",
    "author_id",
    "author_name",
    "avatar",
    "ip_location",
    "location",
    "nickname",
    "sec_uid",
    "user_id",
    "username",
}

TOPIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WHITESPACE_RE = re.compile(r"\s+")


class CommentDataError(ValueError):
    """Raised when a comment record violates the local data contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_timestamp(value: Any, field: str, *, allow_null: bool) -> str | None:
    if value is None or value == "":
        if allow_null:
            return None
        raise CommentDataError(f"{field} is required")
    if not isinstance(value, str):
        raise CommentDataError(f"{field} must be an ISO 8601 string")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommentDataError(f"{field} is not a valid ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CommentDataError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommentDataError(f"{field} must be a string")
    return normalize_text(value)


def _comment_text(raw: dict[str, Any]) -> str:
    value = raw.get("text", "")
    if not isinstance(value, str):
        raise CommentDataError("text must be a string")
    return value.strip()


def _optional_nullable_string(raw: dict[str, Any], field: str) -> str | None:
    value = raw.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise CommentDataError(f"{field} must be a string or null")
    normalized = normalize_text(value)
    return normalized or None


def _non_negative_int(raw: dict[str, Any], field: str) -> int:
    value = raw.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommentDataError(f"{field} must be a non-negative integer")
    return value


def _string_list(raw: dict[str, Any], field: str) -> list[str]:
    value = raw.get(field, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CommentDataError(f"{field} must be an array of strings")
    return sorted({normalize_text(item) for item in value if normalize_text(item)})


def synthetic_comment_id(record: dict[str, Any]) -> str:
    """Build a stable fallback ID without retaining an author's identity."""
    fingerprint_fields = (
        record["platform"],
        record["video_id"],
        record.get("root_comment_id") or "",
        record.get("parent_comment_id") or "",
        record.get("published_at") or "",
        record["author_role"],
        normalize_text(record["text"]).casefold(),
    )
    digest = hashlib.sha256("\x1f".join(fingerprint_fields).encode("utf-8")).hexdigest()
    return f"syn_{digest[:32]}"


def normalize_record(raw: Any, *, default_collected_at: str | None = None) -> dict[str, Any]:
    """Validate a batch or canonical record and return canonical field order."""
    if not isinstance(raw, dict):
        raise CommentDataError("record must be a JSON object")

    unknown = set(raw) - set(CANONICAL_FIELDS)
    personal = unknown & DISALLOWED_PERSONAL_FIELDS
    if personal:
        fields = ", ".join(sorted(personal))
        raise CommentDataError(f"personal fields are not allowed in the local dataset: {fields}")
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise CommentDataError(f"unknown fields: {fields}")

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise CommentDataError(
            f"schema_version must be {SCHEMA_VERSION}, got {schema_version!r}"
        )

    platform = _optional_string(raw, "platform") or PLATFORM
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", platform):
        raise CommentDataError("platform is invalid")

    video_id = _optional_string(raw, "video_id")
    if not video_id:
        raise CommentDataError("video_id is required")
    text = _comment_text(raw)
    if not text:
        raise CommentDataError("text is required")

    parent_comment_id = _optional_nullable_string(raw, "parent_comment_id")
    root_comment_id = _optional_nullable_string(raw, "root_comment_id")
    comment_type = _optional_string(raw, "comment_type")
    if not comment_type:
        comment_type = "reply" if parent_comment_id or root_comment_id else "root"
    if comment_type not in {"root", "reply"}:
        raise CommentDataError("comment_type must be 'root' or 'reply'")
    if comment_type == "root" and (parent_comment_id or root_comment_id):
        raise CommentDataError("root comments cannot have parent_comment_id or root_comment_id")

    author_role = _optional_string(raw, "author_role") or "viewer"
    if author_role not in {"viewer", "creator"}:
        raise CommentDataError("author_role must be 'viewer' or 'creator'")

    published_at = normalize_timestamp(raw.get("published_at"), "published_at", allow_null=True)
    collected_value = raw.get("collected_at") or default_collected_at
    collected_at = normalize_timestamp(collected_value, "collected_at", allow_null=False)

    topic_ids = _string_list(raw, "topic_ids")
    invalid_topics = [topic_id for topic_id in topic_ids if not TOPIC_ID_RE.fullmatch(topic_id)]
    if invalid_topics:
        raise CommentDataError(f"invalid topic_ids: {', '.join(invalid_topics)}")

    manual_tags = _string_list(raw, "manual_tags")
    invalid_tags = [tag for tag in manual_tags if not TAG_RE.fullmatch(tag)]
    if invalid_tags:
        raise CommentDataError(f"invalid manual_tags: {', '.join(invalid_tags)}")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "comment_id": _optional_string(raw, "comment_id"),
        "comment_id_kind": _optional_string(raw, "comment_id_kind"),
        "video_id": video_id,
        "video_title": _optional_string(raw, "video_title"),
        "video_url": _optional_string(raw, "video_url"),
        "parent_comment_id": parent_comment_id,
        "root_comment_id": root_comment_id,
        "comment_type": comment_type,
        "author_role": author_role,
        "text": text,
        "like_count": _non_negative_int(raw, "like_count"),
        "reply_count": _non_negative_int(raw, "reply_count"),
        "published_at": published_at,
        "published_label": _optional_nullable_string(raw, "published_label"),
        "collected_at": collected_at,
        "topic_ids": topic_ids,
        "manual_tags": manual_tags,
        "source_url": _optional_string(raw, "source_url"),
        "collection_batch": _optional_string(raw, "collection_batch"),
    }

    if record["comment_id"]:
        record["comment_id_kind"] = record["comment_id_kind"] or "platform"
    else:
        record["comment_id"] = synthetic_comment_id(record)
        record["comment_id_kind"] = "synthetic"
    if record["comment_id_kind"] not in {"platform", "synthetic"}:
        raise CommentDataError("comment_id_kind must be 'platform' or 'synthetic'")

    for url_field in ("video_url", "source_url"):
        value = record[url_field]
        if value and not value.startswith(("https://", "http://")):
            raise CommentDataError(f"{url_field} must be an HTTP(S) URL")

    return record


def read_jsonl(path: Path) -> Iterator[tuple[int, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                raise CommentDataError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc


def load_records(
    paths: Iterable[Path], *, default_collected_at: str | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in paths:
        if not path.exists():
            errors.append(f"{path}: file does not exist")
            continue
        try:
            rows = list(read_jsonl(path))
        except CommentDataError as exc:
            errors.append(str(exc))
            continue
        for line_number, raw in rows:
            try:
                records.append(
                    normalize_record(raw, default_collected_at=default_collected_at)
                )
            except CommentDataError as exc:
                errors.append(f"{path}:{line_number}: {exc}")
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise CommentDataError(f"validation failed:\n{details}")
    return records


def record_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["platform"], record["comment_id"]


def _merge_snapshot(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    if current["video_id"] != incoming["video_id"]:
        raise CommentDataError(
            "comment_id collision across videos: "
            f"{current['comment_id']} ({current['video_id']} != {incoming['video_id']})"
        )

    if parse_timestamp(incoming["collected_at"]) >= parse_timestamp(current["collected_at"]):
        newer, older = incoming, current
    else:
        newer, older = current, incoming

    merged = dict(newer)
    for field in (
        "video_title",
        "video_url",
        "parent_comment_id",
        "root_comment_id",
        "published_at",
        "published_label",
        "source_url",
        "collection_batch",
    ):
        if not merged.get(field) and older.get(field):
            merged[field] = older[field]
    merged["topic_ids"] = sorted(set(current["topic_ids"]) | set(incoming["topic_ids"]))
    merged["manual_tags"] = sorted(
        set(current["manual_tags"]) | set(incoming["manual_tags"])
    )
    return {field: merged[field] for field in CANONICAL_FIELDS}


def deduplicate_records(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_count = 0
    for record in records:
        key = record_key(record)
        if key in unique:
            duplicate_count += 1
            unique[key] = _merge_snapshot(unique[key], record)
        else:
            unique[key] = record
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            row["video_id"],
            row["published_at"] or "9999",
            0 if row["comment_type"] == "root" else 1,
            row["comment_id"],
        ),
    )
    return ordered, duplicate_count


def validate_record_relations(
    records: Iterable[dict[str, Any]],
    *,
    require_complete_reply_counts: bool = False,
) -> None:
    """Validate hierarchy and reply-count consistency across canonical records.

    A partial collection may contain fewer replies than the root comment reports,
    so equality is optional. Having more captured replies than the platform count
    is always inconsistent and is rejected.
    """
    materialized = list(records)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[str] = []

    for record in materialized:
        key = record_key(record)
        if key in index:
            errors.append(f"duplicate canonical comment key: {key[0]}:{key[1]}")
        else:
            index[key] = record

    replies_by_root: dict[tuple[str, str], int] = {}
    for record in materialized:
        comment_key = record_key(record)
        comment_id = record["comment_id"]
        platform = record["platform"]
        video_id = record["video_id"]

        if record["comment_type"] == "root":
            if record["parent_comment_id"] or record["root_comment_id"]:
                errors.append(
                    f"root comment {comment_id} cannot reference a parent or root"
                )
            continue

        parent_id = record["parent_comment_id"]
        root_id = record["root_comment_id"]
        if not parent_id:
            errors.append(f"reply {comment_id} must have parent_comment_id")
        if not root_id:
            errors.append(f"reply {comment_id} must have root_comment_id")
        if not parent_id or not root_id:
            continue
        if parent_id == comment_id:
            errors.append(f"reply {comment_id} cannot be its own parent")
        if root_id == comment_id:
            errors.append(f"reply {comment_id} cannot be its own root")

        parent = index.get((platform, parent_id))
        root = index.get((platform, root_id))
        if parent is None:
            errors.append(f"reply {comment_id} references missing parent {parent_id}")
        if root is None:
            errors.append(f"reply {comment_id} references missing root {root_id}")
        if parent is None or root is None:
            continue

        if parent["video_id"] != video_id:
            errors.append(
                f"reply {comment_id} and parent {parent_id} belong to different videos"
            )
        if root["video_id"] != video_id:
            errors.append(
                f"reply {comment_id} and root {root_id} belong to different videos"
            )
        if root["comment_type"] != "root":
            errors.append(f"reply {comment_id} points to non-root comment {root_id}")
        if parent["comment_type"] == "root" and parent_id != root_id:
            errors.append(
                f"reply {comment_id} has different parent root {parent_id} and root {root_id}"
            )
        if (
            parent["comment_type"] == "reply"
            and parent["root_comment_id"] != root_id
        ):
            errors.append(
                f"reply {comment_id} and parent {parent_id} belong to different threads"
            )

        root_key = (platform, root_id)
        replies_by_root[root_key] = replies_by_root.get(root_key, 0) + 1

    for record in materialized:
        if record["comment_type"] != "reply":
            continue
        ancestry: set[tuple[str, str]] = set()
        current = record
        while current["comment_type"] == "reply":
            current_key = record_key(current)
            if current_key in ancestry:
                errors.append(
                    f"reply parent cycle detected from {record['comment_id']} "
                    f"at {current['comment_id']}"
                )
                break
            ancestry.add(current_key)
            parent_id = current["parent_comment_id"]
            if not parent_id:
                break
            parent = index.get((current["platform"], parent_id))
            if parent is None:
                break
            current = parent

    for key, record in index.items():
        if record["comment_type"] != "root":
            continue
        captured = replies_by_root.get(key, 0)
        declared = record["reply_count"]
        if captured > declared:
            errors.append(
                f"root comment {record['comment_id']} reports {declared} replies "
                f"but {captured} are stored"
            )
        elif require_complete_reply_counts and captured != declared:
            errors.append(
                f"root comment {record['comment_id']} reports {declared} replies "
                f"but only {captured} are stored"
            )

    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise CommentDataError(f"relation validation failed:\n{details}")


def merge_record_sets(
    existing: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    existing_unique, duplicate_existing = deduplicate_records(existing)
    incoming_list = list(incoming)
    incoming_unique, duplicate_in_batch = deduplicate_records(incoming_list)

    before = {record_key(record): record for record in existing_unique}
    merged, _ = deduplicate_records([*existing_unique, *incoming_unique])
    validate_record_relations(merged)
    after = {record_key(record): record for record in merged}

    new_count = sum(key not in before for key in after)
    updated_count = sum(
        key in before and before[key] != record for key, record in after.items()
    )
    unchanged_count = len(incoming_unique) - new_count - updated_count
    return merged, {
        "input_records": len(incoming_list),
        "new_records": new_count,
        "updated_records": updated_count,
        "unchanged_records": max(0, unchanged_count),
        "duplicate_input_snapshots": duplicate_in_batch,
        "duplicate_existing_snapshots": duplicate_existing,
        "stored_records": len(merged),
    }


def write_jsonl_atomic(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
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
