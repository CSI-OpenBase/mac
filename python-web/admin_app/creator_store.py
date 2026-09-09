"""MySQL synchronization for creator-level and work-level snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .account_data import (
    load_snapshots,
    normalize_audience_snapshot,
    normalize_profile_snapshot,
)
from .config import Settings
from .work_data import load_work_snapshots


class CreatorStoreError(ValueError):
    """Raised when MySQL disagrees with an immutable local snapshot."""


def _mysql_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _scalar(cursor: Any, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) AS count FROM `{table}`")
    row = cursor.fetchone()
    return int(row["count"])


def _record_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CreatorStoreError("stored snapshot contains invalid record_json") from exc
    if not isinstance(value, Mapping):
        raise CreatorStoreError("stored snapshot record_json must be an object")
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _validate_snapshot_conflicts(
    cursor: Any,
    *,
    table: str,
    identity_columns: tuple[str, ...],
    records: Iterable[Mapping[str, Any]],
) -> None:
    allowed = {
        "work_metric_snapshots": ("platform", "work_id", "observed_at"),
        "creator_profile_snapshots": ("platform", "observed_at"),
        "audience_snapshots": (
            "platform",
            "observed_at",
            "dimension_name",
            "segment_name",
        ),
    }
    if allowed.get(table) != identity_columns:
        raise CreatorStoreError(f"unsupported snapshot identity contract: {table}")
    incoming_records = [dict(record) for record in records]
    if not incoming_records:
        return

    def incoming_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
        values: list[Any] = []
        for column in identity_columns:
            source = {
                "dimension_name": "dimension",
                "segment_name": "segment",
            }.get(column, column)
            value = record[source]
            values.append(_mysql_datetime(value) if column == "observed_at" else value)
        return tuple(values)

    incoming = {
        incoming_identity(record): _record_json(record) for record in incoming_records
    }
    platforms = sorted({str(record["platform"]) for record in incoming_records})
    placeholders = ",".join(["%s"] * len(platforms))
    columns = ", ".join(identity_columns)
    cursor.execute(
        f"SELECT {columns}, record_json FROM `{table}` "
        f"WHERE platform IN ({placeholders})",
        platforms,
    )
    for row in cursor.fetchall():
        key = tuple(row[column] for column in identity_columns)
        expected = incoming.get(key)
        if expected is not None and _record_json(row["record_json"]) != expected:
            raise CreatorStoreError(
                f"conflicting MySQL snapshot identity in {table}: {key!r}"
            )


def _latest_works(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    first_seen: dict[tuple[str, str], str] = {}
    for raw in records:
        record = dict(raw)
        key = (record["platform"], record["work_id"])
        first_seen[key] = min(first_seen.get(key, record["observed_at"]), record["observed_at"])
        if key not in latest or record["observed_at"] >= latest[key]["observed_at"]:
            latest[key] = record
    result: list[dict[str, Any]] = []
    for key, record in latest.items():
        result.append({**record, "first_observed_at": first_seen[key]})
    return result


def sync_creator_sources(
    connection: Any, settings: Settings, *, manage_transaction: bool = True
) -> dict[str, int]:
    works = load_work_snapshots(settings.work_snapshots_path)
    profiles = load_snapshots(
        settings.profile_snapshots_path, normalize_profile_snapshot
    )
    audience = load_snapshots(
        settings.audience_snapshots_path, normalize_audience_snapshot
    )
    latest_works = _latest_works(works)
    try:
        if manage_transaction:
            connection.begin()
        with connection.cursor() as cursor:
            _validate_snapshot_conflicts(
                cursor,
                table="work_metric_snapshots",
                identity_columns=("platform", "work_id", "observed_at"),
                records=works,
            )
            _validate_snapshot_conflicts(
                cursor,
                table="creator_profile_snapshots",
                identity_columns=("platform", "observed_at"),
                records=profiles,
            )
            _validate_snapshot_conflicts(
                cursor,
                table="audience_snapshots",
                identity_columns=(
                    "platform",
                    "observed_at",
                    "dimension_name",
                    "segment_name",
                ),
                records=audience,
            )
            before = {
                "work_snapshots": _scalar(cursor, "work_metric_snapshots"),
                "profile_snapshots": _scalar(cursor, "creator_profile_snapshots"),
                "audience_snapshots": _scalar(cursor, "audience_snapshots"),
            }
            if latest_works:
                cursor.executemany(
                    """
                    INSERT INTO creator_works
                        (platform, work_id, work_id_kind, title, tags, published_at,
                         content_type, audit_status, first_observed_at, last_observed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        work_id_kind = IF(VALUES(last_observed_at) >= last_observed_at,
                                          VALUES(work_id_kind), work_id_kind),
                        title = IF(VALUES(last_observed_at) >= last_observed_at,
                                   VALUES(title), title),
                        tags = IF(VALUES(last_observed_at) >= last_observed_at,
                                  VALUES(tags), tags),
                        published_at = IF(VALUES(last_observed_at) >= last_observed_at,
                                          VALUES(published_at), published_at),
                        content_type = IF(VALUES(last_observed_at) >= last_observed_at,
                                          VALUES(content_type), content_type),
                        audit_status = IF(VALUES(last_observed_at) >= last_observed_at,
                                          VALUES(audit_status), audit_status),
                        first_observed_at = LEAST(first_observed_at, VALUES(first_observed_at)),
                        last_observed_at = GREATEST(last_observed_at, VALUES(last_observed_at))
                    """,
                    [
                        (
                            row["platform"], row["work_id"], row["work_id_kind"],
                            row["title"], json.dumps(row["tags"], ensure_ascii=False),
                            _mysql_datetime(row["published_at"]) if row["published_at"] else None,
                            row["content_type"], row["audit_status"],
                            _mysql_datetime(row["first_observed_at"]),
                            _mysql_datetime(row["observed_at"]),
                        )
                        for row in latest_works
                    ],
                )
            if works:
                cursor.executemany(
                    """
                    INSERT IGNORE INTO work_metric_snapshots
                        (platform, work_id, observed_at, view_count, like_count,
                         share_count, comment_count, collect_count, profile_visit_count,
                         follower_gain, completion_rate, five_second_completion_rate,
                         cover_click_rate, two_second_bounce_rate, average_watch_seconds,
                         source_file, source_sheet, record_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row["platform"], row["work_id"], _mysql_datetime(row["observed_at"]),
                            row["view_count"], row["like_count"], row["share_count"],
                            row["comment_count"], row["collect_count"],
                            row["profile_visit_count"], row["follower_gain"],
                            row["completion_rate"], row["five_second_completion_rate"],
                            row["cover_click_rate"], row["two_second_bounce_rate"],
                            row["average_watch_seconds"], row["source_file"],
                            row["source_sheet"],
                            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                        )
                        for row in works
                    ],
                )
            if profiles:
                cursor.executemany(
                    """
                    INSERT IGNORE INTO creator_profile_snapshots
                        (platform, observed_at, display_name, follower_count,
                         following_count, total_like_count, work_count, record_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row["platform"], _mysql_datetime(row["observed_at"]),
                            row["display_name"], row["follower_count"],
                            row["following_count"], row["total_like_count"],
                            row["work_count"],
                            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                        )
                        for row in profiles
                    ],
                )
            if audience:
                cursor.executemany(
                    """
                    INSERT IGNORE INTO audience_snapshots
                        (platform, observed_at, dimension_name, segment_name,
                         share_value, sample_size, record_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            row["platform"], _mysql_datetime(row["observed_at"]),
                            row["dimension"], row["segment"], row["share"],
                            row["sample_size"],
                            json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                        )
                        for row in audience
                    ],
                )
            after = {
                "work_snapshots": _scalar(cursor, "work_metric_snapshots"),
                "profile_snapshots": _scalar(cursor, "creator_profile_snapshots"),
                "audience_snapshots": _scalar(cursor, "audience_snapshots"),
            }
        if manage_transaction:
            connection.commit()
    except Exception:
        if manage_transaction:
            connection.rollback()
        raise
    return {
        "work_count": len(latest_works),
        "inserted_work_snapshots": after["work_snapshots"] - before["work_snapshots"],
        "inserted_profile_snapshots": after["profile_snapshots"] - before["profile_snapshots"],
        "inserted_audience_snapshots": after["audience_snapshots"] - before["audience_snapshots"],
    }
