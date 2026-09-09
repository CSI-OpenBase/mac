"""Privacy-minimized creator profile and aggregate audience snapshots."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


class AccountDataError(ValueError):
    """Raised when an account-level aggregate record is invalid."""


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountDataError("observed_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountDataError("observed_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AccountDataError("observed_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountDataError(f"{field} must be a non-negative integer")
    return value


def normalize_profile_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(raw.get("platform") or "douyin").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", platform):
        raise AccountDataError("platform is invalid")
    allowed = {
        "schema_version", "platform", "observed_at", "display_name",
        "follower_count", "following_count", "total_like_count", "work_count",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise AccountDataError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    return {
        "schema_version": 1,
        "platform": platform,
        "observed_at": _timestamp(raw.get("observed_at")),
        "display_name": str(raw.get("display_name") or "").strip()[:120],
        "follower_count": _count(raw.get("follower_count", 0), "follower_count"),
        "following_count": _count(raw.get("following_count", 0), "following_count"),
        "total_like_count": _count(raw.get("total_like_count", 0), "total_like_count"),
        "work_count": _count(raw.get("work_count", 0), "work_count"),
    }


def normalize_audience_snapshot(raw: Mapping[str, Any]) -> dict[str, Any]:
    platform = str(raw.get("platform") or "douyin").strip().lower()
    dimension = str(raw.get("dimension") or "").strip()
    segment = str(raw.get("segment") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", platform):
        raise AccountDataError("platform is invalid")
    if not dimension or not segment or len(dimension) > 64 or len(segment) > 120:
        raise AccountDataError("audience dimension and segment are required")
    share = raw.get("share")
    if isinstance(share, bool) or not isinstance(share, (int, float)):
        raise AccountDataError("audience share must be numeric")
    numeric_share = float(share)
    if not math.isfinite(numeric_share) or not 0 <= numeric_share <= 1:
        raise AccountDataError("audience share must be between 0 and 1")
    sample_size = raw.get("sample_size")
    if sample_size is not None:
        sample_size = _count(sample_size, "sample_size")
    return {
        "schema_version": 1,
        "platform": platform,
        "observed_at": _timestamp(raw.get("observed_at")),
        "dimension": dimension,
        "segment": segment,
        "share": round(numeric_share, 8),
        "sample_size": sample_size,
    }


def load_snapshots(
    path: Path, normalizer: Callable[[Mapping[str, Any]], dict[str, Any]]
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AccountDataError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(raw, dict):
                raise AccountDataError(f"{path}:{line_number}: record must be an object")
            records.append(normalizer(raw))
    return records


def merge_snapshots(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    identity_fields: tuple[str, ...],
) -> tuple[list[dict[str, Any]], int]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    duplicate_count = 0
    for record in [*existing, *incoming]:
        identity = tuple(record[field] for field in identity_fields)
        current = merged.get(identity)
        if current is not None:
            if current != record:
                raise AccountDataError(
                    "conflicting account snapshot at "
                    + ":".join(str(value) for value in identity)
                )
            duplicate_count += 1
            continue
        merged[identity] = record
    return [merged[key] for key in sorted(merged)], duplicate_count


def write_snapshots(path: Path, records: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
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


def parse_snapshot_bytes(
    content: bytes,
    *,
    kind: str,
    platform: str,
) -> list[dict[str, Any]]:
    normalizers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
        "profile": normalize_profile_snapshot,
        "audience": normalize_audience_snapshot,
    }
    normalizer = normalizers.get(kind)
    if normalizer is None:
        raise AccountDataError("snapshot kind must be profile or audience")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AccountDataError("snapshot JSONL must use UTF-8") from exc
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AccountDataError(f"line {line_number}: invalid JSON") from exc
        if not isinstance(raw, dict):
            raise AccountDataError(f"line {line_number}: record must be an object")
        record = normalizer({"platform": platform, **raw})
        if record["platform"] != platform:
            raise AccountDataError("snapshot platform does not match the workspace")
        result.append(record)
    if not result:
        raise AccountDataError("snapshot file contains no records")
    return result
