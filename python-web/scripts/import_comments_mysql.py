#!/usr/bin/env python3
"""Import the active workspace comment archive into local MySQL."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from admin_app.archive_lock import archive_lock
from admin_app.schema_contract import (
    REQUIRED_TABLES,
    SchemaContractError,
    validate_schema_contract,
)

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only on an unprepared machine
    pymysql = None
    DictCursor = None

from scripts.comment_data import (
    CANONICAL_FIELDS,
    CommentDataError,
    deduplicate_records,
    load_records,
    parse_timestamp,
    validate_record_relations,
)


DEFAULT_COMMENTS: Path | None = None
DEFAULT_TARGETS: Path | None = None
DEFAULT_PROGRESS: Path | None = None
DEFAULT_SCHEMA = REPOSITORY_ROOT / "admin_app" / "resources" / "mysql-schema.sql"
DEFAULT_PASSWORD_ENV = "CSI_OPENBASE_DB_PASSWORD"
PLATFORM = "douyin"
VALID_STATUSES = {"pending", "partial", "complete", "blocked"}
SNAPSHOT_OBSERVATION_FIELDS = tuple(
    field
    for field in CANONICAL_FIELDS
    if field not in {"collected_at", "collection_batch"}
)
EXPECTED_TABLE_COLLATION = "utf8mb4_0900_ai_ci"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
class ImportDataError(ValueError):
    """Raised when an import source or database invariant is invalid."""


@dataclass(frozen=True)
class ImportPayload:
    comments: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    duplicate_source_count: int
    targets: dict[str, Any]
    progress: dict[str, Any]
    source_sha256: str
    targets_sha256: str
    progress_sha256: str


def validate_identifier(value: str, *, label: str = "identifier") -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ImportDataError(
            f"{label} must start with a letter or underscore and contain only "
            "ASCII letters, digits, or underscores (maximum 64 characters)"
        )
    return value


def quote_identifier(value: str) -> str:
    return f"`{validate_identifier(value)}`"


def resolve_password(
    environment: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    environment = os.environ if environment is None else environment
    password = environment.get(DEFAULT_PASSWORD_ENV)
    if password is None:
        password = prompt("MySQL password: ")
    if not password:
        raise ImportDataError("MySQL password cannot be empty")
    return password


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _batch_paths(comments_path: Path) -> list[Path]:
    batches_dir = comments_path.resolve().parent / "batches"
    if not batches_dir.is_dir():
        return []
    return sorted(
        path
        for path in batches_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jsonl", ".ndjson"}
    )


def _archive_sha256(comments_path: Path) -> str:
    """Hash every file that contributes, or may later contribute, a snapshot."""
    comments_path = comments_path.resolve()
    digest = hashlib.sha256(b"douyin-comment-archive-v1\0")
    for path in [comments_path, *_batch_paths(comments_path)]:
        label = (
            comments_path.name
            if path == comments_path
            else f"batches/{path.name}"
        )
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def to_mysql_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = parse_timestamp(value)
        except ValueError as exc:
            raise ImportDataError(f"invalid ISO 8601 timestamp: {value!r}") from exc
    else:
        raise ImportDataError(f"timestamp must be a string, datetime, or null: {value!r}")
    if parsed.tzinfo is None:
        raise ImportDataError("timestamp must include a timezone offset")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ImportDataError(f"{label} does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImportDataError(f"{label} is not valid JSON: {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ImportDataError(f"{label} must contain a JSON object")
    return value


def require_string(value: Any, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise ImportDataError(f"{field} must be {suffix}")
    return value.strip() if not allow_empty else value


def require_non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ImportDataError(f"{field} must be a non-negative integer")
    return value


def require_status(value: Any, *, field: str) -> str:
    status = require_string(value, field=field)
    if status not in VALID_STATUSES:
        raise ImportDataError(
            f"{field} must be one of: {', '.join(sorted(VALID_STATUSES))}"
        )
    return status


def validate_target_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != 1:
        raise ImportDataError("target manifest schema_version must be 1")
    scope_id = require_string(manifest.get("scope_id"), field="targets.scope_id")
    require_string(manifest.get("scope_note"), field="targets.scope_note", allow_empty=True)
    to_mysql_datetime(require_string(manifest.get("generated_at"), field="targets.generated_at"))

    collections = manifest.get("collections")
    videos = manifest.get("videos")
    if not isinstance(collections, list):
        raise ImportDataError("targets.collections must be an array")
    if not isinstance(videos, list):
        raise ImportDataError("targets.videos must be an array")
    if videos and not collections:
        raise ImportDataError("targets.collections cannot be empty when videos exist")

    collection_ids: set[str] = set()
    collection_episode_counts: dict[str, int] = {}
    for index, collection in enumerate(collections):
        if not isinstance(collection, dict):
            raise ImportDataError(f"targets.collections[{index}] must be an object")
        collection_id = require_string(
            collection.get("collection_id"),
            field=f"targets.collections[{index}].collection_id",
        )
        if collection_id in collection_ids:
            raise ImportDataError(f"duplicate collection_id in target manifest: {collection_id}")
        collection_ids.add(collection_id)
        require_string(collection.get("name"), field=f"targets.collections[{index}].name")
        collection_episode_counts[collection_id] = require_non_negative_int(
            collection.get("episode_count"),
            field=f"targets.collections[{index}].episode_count",
        )

    video_ids: set[str] = set()
    collection_episodes: set[tuple[str, int]] = set()
    collection_video_counts: dict[str, int] = defaultdict(int)
    complete_count = 0
    for index, video in enumerate(videos):
        if not isinstance(video, dict):
            raise ImportDataError(f"targets.videos[{index}] must be an object")
        prefix = f"targets.videos[{index}]"
        video_id = require_string(video.get("video_id"), field=f"{prefix}.video_id")
        if video_id in video_ids:
            raise ImportDataError(f"duplicate video_id in target manifest: {video_id}")
        video_ids.add(video_id)
        collection_id = require_string(
            video.get("collection_id"), field=f"{prefix}.collection_id"
        )
        if collection_id not in collection_ids:
            raise ImportDataError(
                f"{prefix}.collection_id references an unknown collection: {collection_id}"
            )
        collection_video_counts[collection_id] += 1
        require_string(video.get("collection_name"), field=f"{prefix}.collection_name")
        episode = require_non_negative_int(video.get("episode"), field=f"{prefix}.episode")
        if episode == 0:
            raise ImportDataError(f"{prefix}.episode must be greater than zero")
        episode_key = (collection_id, episode)
        if episode_key in collection_episodes:
            raise ImportDataError(
                f"duplicate episode {episode} in collection {collection_id}"
            )
        collection_episodes.add(episode_key)
        require_string(video.get("title"), field=f"{prefix}.title", allow_empty=True)
        require_string(video.get("video_url"), field=f"{prefix}.video_url")
        require_string(video.get("card_metric", ""), field=f"{prefix}.card_metric", allow_empty=True)
        if require_status(video.get("status"), field=f"{prefix}.status") == "complete":
            complete_count += 1

    declared_target_count = require_non_negative_int(
        manifest.get("target_video_count"), field="targets.target_video_count"
    )
    declared_complete_count = require_non_negative_int(
        manifest.get("completed_video_count"), field="targets.completed_video_count"
    )
    if declared_target_count != len(videos):
        raise ImportDataError(
            f"targets.target_video_count is {declared_target_count}, expected {len(videos)}"
        )
    if declared_complete_count != complete_count:
        raise ImportDataError(
            f"targets.completed_video_count is {declared_complete_count}, "
            f"expected {complete_count}"
        )
    for collection_id, episode_count in collection_episode_counts.items():
        actual = collection_video_counts[collection_id]
        if episode_count != actual:
            raise ImportDataError(
                f"collection {collection_id} declares {episode_count} episodes, "
                f"but the target manifest contains {actual}"
            )
    return manifest


def validate_progress(
    progress: dict[str, Any], *, targets: dict[str, Any]
) -> dict[str, Any]:
    if progress.get("schema_version") != 1:
        raise ImportDataError("collection progress schema_version must be 1")
    scope_id = require_string(progress.get("scope_id"), field="progress.scope_id")
    if scope_id != targets["scope_id"]:
        raise ImportDataError(
            f"progress scope_id {scope_id!r} does not match targets {targets['scope_id']!r}"
        )
    target_ids = {video["video_id"] for video in targets["videos"]}
    target_count = require_non_negative_int(
        progress.get("target_video_count"), field="progress.target_video_count"
    )
    if target_count != len(target_ids):
        raise ImportDataError(
            f"progress.target_video_count is {target_count}, expected {len(target_ids)}"
        )
    updated_at = require_string(progress.get("updated_at"), field="progress.updated_at")
    to_mysql_datetime(updated_at)
    videos = progress.get("videos")
    if not isinstance(videos, dict):
        raise ImportDataError("progress.videos must be an object keyed by video_id")

    completed = 0
    stored_total = 0
    for video_id, item in videos.items():
        require_string(video_id, field="progress video_id")
        if video_id not in target_ids:
            raise ImportDataError(f"progress contains a video outside the target manifest: {video_id}")
        if not isinstance(item, dict):
            raise ImportDataError(f"progress.videos.{video_id} must be an object")
        prefix = f"progress.videos.{video_id}"
        require_string(item.get("title"), field=f"{prefix}.title", allow_empty=True)
        require_string(item.get("url"), field=f"{prefix}.url")
        status = require_status(item.get("status"), field=f"{prefix}.status")
        completed += int(status == "complete")
        require_non_negative_int(
            item.get("visible_comment_count"), field=f"{prefix}.visible_comment_count"
        )
        stored_total += require_non_negative_int(
            item.get("stored_record_count"), field=f"{prefix}.stored_record_count"
        )
        require_string(item.get("last_batch", ""), field=f"{prefix}.last_batch", allow_empty=True)
        if item.get("last_collected_at") is not None:
            to_mysql_datetime(item["last_collected_at"])
        require_string(item.get("notes", ""), field=f"{prefix}.notes", allow_empty=True)

    declared_completed = require_non_negative_int(
        progress.get("completed_video_count"), field="progress.completed_video_count"
    )
    declared_stored = require_non_negative_int(
        progress.get("stored_record_count"), field="progress.stored_record_count"
    )
    if declared_completed != completed:
        raise ImportDataError(
            f"progress.completed_video_count is {declared_completed}, expected {completed}"
        )
    if declared_stored != stored_total:
        raise ImportDataError(
            f"progress.stored_record_count is {declared_stored}, expected {stored_total}"
        )
    return progress


def _load_snapshot_history(
    comments_path: Path,
    canonical_records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Recover all archived observations while keeping canonical as a fallback.

    The canonical archive intentionally keeps only the newest state for each
    comment. A failed MySQL sync must not make an older observation unreachable
    after a later collection replaces that state, so archived batches are the
    primary snapshot source. Canonical rows cover legacy data that predates the
    batch archive. Exact observation identities are de-duplicated before import.
    """
    snapshots_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    canonical_by_key = {
        (record["platform"], record["comment_id"]): record
        for record in canonical_records
    }
    for batch_path in _batch_paths(comments_path):
        for record in load_records([batch_path]):
            key = (record["platform"], record["comment_id"])
            canonical = canonical_by_key.get(key)
            if canonical is None:
                # A capture can leave a batch behind if the process exits
                # before canonical ingestion. It is not importable yet.
                continue
            if record["video_id"] != canonical["video_id"]:
                raise ImportDataError(
                    "archived comment_id collision across videos: "
                    f"{record['comment_id']} "
                    f"({record['video_id']} != {canonical['video_id']})"
                )
            if parse_timestamp(record["collected_at"]) > parse_timestamp(
                canonical["collected_at"]
            ):
                continue
            if not record["collection_batch"]:
                record = dict(record)
                record["collection_batch"] = batch_path.stem
            identity = (
                record["platform"],
                record["comment_id"],
                record["collected_at"],
            )
            existing = snapshots_by_identity.get(identity)
            if existing is not None and any(
                existing[field] != record[field]
                for field in SNAPSHOT_OBSERVATION_FIELDS
            ):
                raise ImportDataError(
                    "conflicting archived snapshot content for "
                    f"{record['platform']}:{record['comment_id']} at "
                    f"{record['collected_at']}"
                )
            snapshots_by_identity[identity] = record

    for record in canonical_records:
        identity = (
            record["platform"],
            record["comment_id"],
            record["collected_at"],
        )
        if identity not in snapshots_by_identity:
            snapshots_by_identity[identity] = record

    return [
        snapshots_by_identity[identity]
        for identity in sorted(
            snapshots_by_identity,
            key=lambda item: (item[2], item[0], item[1]),
        )
    ]


def load_import_payload(
    comments_path: Path, targets_path: Path, progress_path: Path
) -> ImportPayload:
    canonical_records = load_records([comments_path])
    comments, duplicate_count = deduplicate_records(canonical_records)
    snapshots = _load_snapshot_history(comments_path, comments)
    unexpected_platforms = sorted(
        {record["platform"] for record in snapshots if record["platform"] != PLATFORM}
    )
    if unexpected_platforms:
        raise ImportDataError(
            "comment source contains unsupported platforms: "
            + ", ".join(unexpected_platforms)
        )
    validate_record_relations(comments)
    targets = validate_target_manifest(load_json_object(targets_path, label="target manifest"))
    progress = validate_progress(
        load_json_object(progress_path, label="collection progress"), targets=targets
    )
    return ImportPayload(
        comments=comments,
        snapshots=snapshots,
        duplicate_source_count=duplicate_count,
        targets=targets,
        progress=progress,
        source_sha256=_archive_sha256(comments_path),
        targets_sha256=file_sha256(targets_path),
        progress_sha256=file_sha256(progress_path),
    )


def parent_first(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return comments in a stable topological order over direct-parent edges."""
    index = {(row["platform"], row["comment_id"]): row for row in records}
    state: dict[tuple[str, str], int] = {}
    ordered: list[dict[str, Any]] = []

    def visit(row: dict[str, Any]) -> None:
        key = (row["platform"], row["comment_id"])
        marker = state.get(key, 0)
        if marker == 2:
            return
        if marker == 1:
            raise ImportDataError(f"comment parent cycle detected at {key[0]}:{key[1]}")
        state[key] = 1
        parent_id = row.get("parent_comment_id")
        if parent_id:
            parent = index.get((row["platform"], parent_id))
            if parent is None:
                raise ImportDataError(
                    f"comment {row['comment_id']} references missing parent {parent_id}"
                )
            visit(parent)
        state[key] = 2
        ordered.append(row)

    for record in records:
        visit(record)
    return ordered


def split_sql_statements(sql: str) -> list[str]:
    """Split the repository DDL without treating quoted semicolons as delimiters."""
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
                buffer.append(char)
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is None and char == "-" and following == "-":
            line_comment = True
            index += 2
            continue
        if quote is None and char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if quote is None and char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if quote is not None:
            buffer.append(char)
            if char == "\\" and quote in ("'", '"') and following:
                buffer.append(following)
                index += 2
                continue
            if char == quote:
                if following == quote:
                    buffer.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
        else:
            buffer.append(char)
        index += 1
    if quote or block_comment:
        raise ImportDataError("unterminated quote or block comment in schema SQL")
    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


def connect_mysql(
    *, host: str, port: int, user: str, password: str, database: str | None
):
    if pymysql is None:
        raise ImportDataError("PyMySQL is required: install it with `python -m pip install PyMySQL`")
    options: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DictCursor,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
    }
    if database is not None:
        options["database"] = database
    return pymysql.connect(**options)


def create_database_if_missing(
    *, host: str, port: int, user: str, password: str, database: str
) -> None:
    quoted_database = quote_identifier(database)
    connection = connect_mysql(
        host=host, port=port, user=user, password=password, database=None
    )
    try:
        connection.autocommit(True)
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {quoted_database} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
    finally:
        connection.close()


def apply_schema(connection: Any, schema_path: Path) -> None:
    try:
        statements = split_sql_statements(schema_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ImportDataError(f"schema file does not exist: {schema_path}") from exc
    if not statements:
        raise ImportDataError(f"schema file contains no SQL statements: {schema_path}")
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def bind_workspace_identity(
    connection: Any, *, workspace_slug: str, platform: str
) -> None:
    """Bind the selected database to one workspace before importing data."""

    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT workspace_slug, platform FROM workspace_identity "
                "WHERE singleton_id = 1 FOR UPDATE"
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO workspace_identity "
                    "(singleton_id, workspace_slug, platform) VALUES (1, %s, %s)",
                    (workspace_slug, platform),
                )
            else:
                actual = (str(row["workspace_slug"]), str(row["platform"]))
                expected = (workspace_slug, platform)
                if actual != expected:
                    raise ImportDataError(
                        "database workspace identity mismatch: "
                        f"stored {actual[0]!r} ({actual[1]}), "
                        f"active {expected[0]!r} ({expected[1]})"
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def assert_existing_workspace_identity(
    connection: Any, *, workspace_slug: str, platform: str
) -> None:
    """Reject a foreign database before applying schema DDL."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS count FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'workspace_identity'"
        )
        row = cursor.fetchone() or {}
        if int(row.get("count", 0)) == 0:
            return
        cursor.execute(
            "SELECT COLUMN_NAME AS column_name FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'workspace_identity'"
        )
        columns = {str(row["column_name"]) for row in cursor.fetchall()}
        missing = sorted({"singleton_id", "workspace_slug", "platform"} - columns)
        if missing:
            raise ImportDataError(
                "existing workspace_identity table is malformed; missing columns: "
                + ", ".join(missing)
            )
        cursor.execute(
            "SELECT workspace_slug, platform FROM workspace_identity "
            "WHERE singleton_id = 1"
        )
        identity = cursor.fetchone()
    if identity is None:
        return
    actual = (str(identity["workspace_slug"]), str(identity["platform"]))
    expected = (workspace_slug, platform)
    if actual != expected:
        raise ImportDataError(
            "database workspace identity mismatch: "
            f"stored {actual[0]!r} ({actual[1]}), "
            f"active {expected[0]!r} ({expected[1]})"
        )


COLLECTION_UPSERT = """
INSERT INTO collections
    (platform, collection_id, name, declared_episode_count, source_scope_id,
     manifest_generated_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    name = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
              VALUES(name), name),
    declared_episode_count = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
                                VALUES(declared_episode_count), declared_episode_count),
    source_scope_id = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
                         VALUES(source_scope_id), source_scope_id),
    manifest_generated_at = GREATEST(manifest_generated_at, VALUES(manifest_generated_at))
"""

VIDEO_UPSERT = """
INSERT INTO videos
    (platform, video_id, title, video_url, first_seen_at, last_seen_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    title = IF(VALUES(last_seen_at) >= last_seen_at AND VALUES(title) <> '',
               VALUES(title), title),
    video_url = IF(VALUES(last_seen_at) >= last_seen_at AND VALUES(video_url) <> '',
                   VALUES(video_url), video_url),
    first_seen_at = LEAST(first_seen_at, VALUES(first_seen_at)),
    last_seen_at = GREATEST(last_seen_at, VALUES(last_seen_at))
"""

COLLECTION_VIDEO_UPSERT = """
INSERT INTO collection_videos
    (platform, collection_id, video_id, episode, card_metric, target_status,
     manifest_generated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    episode = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
                 VALUES(episode), episode),
    card_metric = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
                     VALUES(card_metric), card_metric),
    target_status = IF(VALUES(manifest_generated_at) >= manifest_generated_at,
                       VALUES(target_status), target_status),
    manifest_generated_at = GREATEST(manifest_generated_at, VALUES(manifest_generated_at))
"""

PROGRESS_UPSERT = """
INSERT INTO collection_progress
    (platform, scope_id, video_id, status, visible_comment_count,
     stored_record_count, last_batch, last_collected_at, notes, source_updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    status = IF(VALUES(source_updated_at) >= source_updated_at, VALUES(status), status),
    visible_comment_count = IF(VALUES(source_updated_at) >= source_updated_at,
                               VALUES(visible_comment_count), visible_comment_count),
    stored_record_count = IF(VALUES(source_updated_at) >= source_updated_at,
                             VALUES(stored_record_count), stored_record_count),
    last_batch = IF(VALUES(source_updated_at) >= source_updated_at,
                    VALUES(last_batch), last_batch),
    last_collected_at = IF(VALUES(source_updated_at) >= source_updated_at,
                           VALUES(last_collected_at), last_collected_at),
    notes = IF(VALUES(source_updated_at) >= source_updated_at, VALUES(notes), notes),
    source_updated_at = GREATEST(source_updated_at, VALUES(source_updated_at))
"""

COMMENT_UPSERT = """
INSERT INTO comments
    (platform, comment_id, schema_version, comment_id_kind, video_id,
     parent_comment_id, root_comment_id, comment_type, author_role, text,
     like_count, reply_count, published_at, published_label,
     first_collected_at, last_collected_at, source_url, collection_batch)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    schema_version = IF(VALUES(last_collected_at) >= last_collected_at,
                        VALUES(schema_version), schema_version),
    comment_id_kind = IF(VALUES(last_collected_at) >= last_collected_at,
                         VALUES(comment_id_kind), comment_id_kind),
    video_id = IF(VALUES(last_collected_at) >= last_collected_at,
                  VALUES(video_id), video_id),
    parent_comment_id = IF(VALUES(last_collected_at) >= last_collected_at,
                           VALUES(parent_comment_id), parent_comment_id),
    root_comment_id = IF(VALUES(last_collected_at) >= last_collected_at,
                         VALUES(root_comment_id), root_comment_id),
    comment_type = IF(VALUES(last_collected_at) >= last_collected_at,
                      VALUES(comment_type), comment_type),
    author_role = IF(VALUES(last_collected_at) >= last_collected_at,
                     VALUES(author_role), author_role),
    text = IF(VALUES(last_collected_at) >= last_collected_at, VALUES(text), text),
    like_count = IF(VALUES(last_collected_at) >= last_collected_at,
                    VALUES(like_count), like_count),
    reply_count = IF(VALUES(last_collected_at) >= last_collected_at,
                     VALUES(reply_count), reply_count),
    published_at = IF(VALUES(last_collected_at) >= last_collected_at,
                      VALUES(published_at), published_at),
    published_label = IF(VALUES(last_collected_at) >= last_collected_at,
                         VALUES(published_label), published_label),
    source_url = IF(VALUES(last_collected_at) >= last_collected_at,
                    VALUES(source_url), source_url),
    collection_batch = IF(VALUES(last_collected_at) >= last_collected_at,
                          VALUES(collection_batch), collection_batch),
    first_collected_at = LEAST(first_collected_at, VALUES(first_collected_at)),
    last_collected_at = GREATEST(last_collected_at, VALUES(last_collected_at))
"""

SNAPSHOT_INSERT = """
INSERT INTO comment_snapshots
    (platform, comment_id, collected_at, schema_version, comment_id_kind,
     video_id, video_title, video_url, parent_comment_id, root_comment_id,
     comment_type, author_role, text, like_count, reply_count, published_at,
     published_label, source_url, collection_batch, record_json)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE snapshot_id = snapshot_id
"""


def build_video_rows(payload: ImportPayload) -> list[tuple[Any, ...]]:
    generated_at = to_mysql_datetime(payload.targets["generated_at"])
    aggregated: dict[str, dict[str, Any]] = {}
    for video in payload.targets["videos"]:
        aggregated[video["video_id"]] = {
            "title": video["title"],
            "url": video["video_url"],
            "first": generated_at,
            "last": generated_at,
        }
    for record in payload.comments:
        seen_at = to_mysql_datetime(record["collected_at"])
        current = aggregated.get(record["video_id"])
        if current is None:
            aggregated[record["video_id"]] = {
                "title": record["video_title"],
                "url": record["video_url"],
                "first": seen_at,
                "last": seen_at,
            }
            continue
        current["first"] = min(current["first"], seen_at)
        if seen_at >= current["last"]:
            if record["video_title"]:
                current["title"] = record["video_title"]
            if record["video_url"]:
                current["url"] = record["video_url"]
            current["last"] = seen_at
    return [
        (PLATFORM, video_id, item["title"], item["url"], item["first"], item["last"])
        for video_id, item in sorted(aggregated.items())
    ]


def fetch_existing_comment_state(
    cursor: Any, records: Sequence[dict[str, Any]], *, chunk_size: int = 500
) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for record in records:
        grouped[record["platform"]].append(record["comment_id"])
    existing: dict[tuple[str, str], dict[str, Any]] = {}
    for platform, comment_ids in grouped.items():
        for offset in range(0, len(comment_ids), chunk_size):
            chunk = comment_ids[offset : offset + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                "SELECT platform, comment_id, video_id, last_collected_at "
                f"FROM comments WHERE platform = %s AND comment_id IN ({placeholders})",
                [platform, *chunk],
            )
            for row in cursor.fetchall():
                existing[(row["platform"], row["comment_id"])] = row
    return existing


def validate_existing_comment_snapshots(
    cursor: Any,
    records: Sequence[dict[str, Any]],
    *,
    chunk_size: int = 500,
) -> None:
    """Reject a stored snapshot identity whose observed content has changed."""

    incoming = {
        (
            record["platform"],
            record["comment_id"],
            to_mysql_datetime(record["collected_at"]),
        ): record
        for record in records
    }
    grouped: dict[str, list[str]] = defaultdict(list)
    for platform, comment_id, _ in incoming:
        grouped[platform].append(comment_id)
    for platform, comment_ids in grouped.items():
        unique_ids = sorted(set(comment_ids))
        for offset in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[offset : offset + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                "SELECT platform, comment_id, collected_at, record_json "
                "FROM comment_snapshots WHERE platform = %s "
                f"AND comment_id IN ({placeholders})",
                [platform, *chunk],
            )
            for row in cursor.fetchall():
                key = (row["platform"], row["comment_id"], row["collected_at"])
                candidate = incoming.get(key)
                if candidate is None:
                    continue
                stored = row["record_json"]
                if isinstance(stored, str):
                    try:
                        stored = json.loads(stored)
                    except json.JSONDecodeError as exc:
                        raise ImportDataError(
                            "stored comment snapshot contains invalid record_json"
                        ) from exc
                if not isinstance(stored, Mapping) or any(
                    stored.get(field) != candidate.get(field)
                    for field in SNAPSHOT_OBSERVATION_FIELDS
                ):
                    raise ImportDataError(
                        "conflicting MySQL comment snapshot for "
                        f"{candidate['platform']}:{candidate['comment_id']} at "
                        f"{candidate['collected_at']}"
                    )


def validate_collection_video_mappings(
    incoming: Sequence[Mapping[str, Any]],
    existing: Sequence[Mapping[str, Any]],
) -> None:
    """Reject ambiguous changes across the mapping table's two unique keys."""
    by_video = {
        (row["platform"], row["collection_id"], row["video_id"]): row
        for row in existing
    }
    by_episode = {
        (row["platform"], row["collection_id"], row["episode"]): row
        for row in existing
        if row["episode"] is not None
    }
    for row in incoming:
        platform = row.get("platform", PLATFORM)
        collection_id = row["collection_id"]
        video_id = row["video_id"]
        episode = row["episode"]
        previous_video = by_video.get((platform, collection_id, video_id))
        if previous_video is not None and previous_video["episode"] != episode:
            raise ImportDataError(
                "collection mapping conflict: "
                f"{collection_id}/{video_id} is stored as episode "
                f"{previous_video['episode']}, incoming episode is {episode}"
            )
        previous_episode = by_episode.get((platform, collection_id, episode))
        if previous_episode is not None and previous_episode["video_id"] != video_id:
            raise ImportDataError(
                "collection mapping conflict: "
                f"{collection_id} episode {episode} is stored as video "
                f"{previous_episode['video_id']}, incoming video is {video_id}"
            )


def prune_removed_target_state(
    cursor: Any,
    *,
    payload: ImportPayload,
    manifest_generated_at: datetime,
    progress_updated_at: datetime,
) -> dict[str, int]:
    """Replace target state only when the incoming scope is globally current."""

    scope_id = payload.targets["scope_id"]
    desired_collections = {
        item["collection_id"] for item in payload.targets["collections"]
    }
    desired_mappings = {
        (item["collection_id"], item["video_id"])
        for item in payload.targets["videos"]
    }
    desired_progress = set(payload.progress["videos"])
    removed = {"collections": 0, "collection_videos": 0, "progress": 0}

    cursor.execute(
        "SELECT MAX(manifest_generated_at) AS latest_at FROM collections "
        "WHERE platform = %s",
        (PLATFORM,),
    )
    row = cursor.fetchone() or {}
    latest_manifest = row.get("latest_at")

    cursor.execute(
        "SELECT MAX(source_updated_at) AS latest_at FROM collection_progress "
        "WHERE platform = %s",
        (PLATFORM,),
    )
    row = cursor.fetchone() or {}
    latest_progress = row.get("latest_at")

    # Progress is removed first so an old scope cannot survive after its target
    # collections have been replaced. Videos themselves remain as comment FKs.
    if latest_progress is None or progress_updated_at >= latest_progress:
        cursor.execute(
            "SELECT scope_id, video_id FROM collection_progress "
            "WHERE platform = %s",
            (PLATFORM,),
        )
        stale_progress = [
            (PLATFORM, row["scope_id"], row["video_id"])
            for row in cursor.fetchall()
            if row["scope_id"] != scope_id or row["video_id"] not in desired_progress
        ]
        if stale_progress:
            removed["progress"] = cursor.executemany(
                "DELETE FROM collection_progress WHERE platform = %s "
                "AND scope_id = %s AND video_id = %s",
                stale_progress,
            )

    if latest_manifest is None or manifest_generated_at >= latest_manifest:
        cursor.execute(
            "SELECT c.source_scope_id, cv.collection_id, cv.video_id "
            "FROM collection_videos AS cv "
            "JOIN collections AS c ON c.platform = cv.platform "
            "AND c.collection_id = cv.collection_id "
            "WHERE c.platform = %s",
            (PLATFORM,),
        )
        stale_mappings = [
            (PLATFORM, row["collection_id"], row["video_id"])
            for row in cursor.fetchall()
            if row["source_scope_id"] != scope_id
            or (row["collection_id"], row["video_id"]) not in desired_mappings
        ]
        if stale_mappings:
            removed["collection_videos"] = cursor.executemany(
                "DELETE FROM collection_videos WHERE platform = %s "
                "AND collection_id = %s AND video_id = %s",
                stale_mappings,
            )
        cursor.execute(
            "SELECT source_scope_id, collection_id FROM collections "
            "WHERE platform = %s",
            (PLATFORM,),
        )
        stale_collections = [
            (PLATFORM, row["source_scope_id"], row["collection_id"])
            for row in cursor.fetchall()
            if row["source_scope_id"] != scope_id
            or row["collection_id"] not in desired_collections
        ]
        if stale_collections:
            removed["collections"] = cursor.executemany(
                "DELETE FROM collections WHERE platform = %s "
                "AND source_scope_id = %s AND collection_id = %s",
                stale_collections,
            )
    return removed


def fetch_existing_collection_video_mappings(
    cursor: Any, incoming: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in incoming:
        grouped[row.get("platform", PLATFORM)].add(row["collection_id"])
    existing: list[dict[str, Any]] = []
    for platform, collection_ids in grouped.items():
        ordered_ids = sorted(collection_ids)
        placeholders = ",".join(["%s"] * len(ordered_ids))
        cursor.execute(
            "SELECT platform, collection_id, video_id, episode "
            "FROM collection_videos WHERE platform = %s "
            f"AND collection_id IN ({placeholders})",
            [platform, *ordered_ids],
        )
        existing.extend(cursor.fetchall())
    return existing


def accepted_current_keys(
    records: Sequence[dict[str, Any]],
    existing: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Choose snapshots allowed to replace the queryable current state."""
    accepted: list[tuple[str, str]] = []
    for record in records:
        key = (record["platform"], record["comment_id"])
        current = existing.get(key)
        if current is not None and current["video_id"] != record["video_id"]:
            raise ImportDataError(
                "comment_id collision across videos in MySQL: "
                f"{record['comment_id']} ({current['video_id']} != {record['video_id']})"
            )
        incoming_at = to_mysql_datetime(record["collected_at"])
        if current is None or incoming_at >= current["last_collected_at"]:
            accepted.append(key)
    return accepted


def delete_current_labels(
    cursor: Any,
    table: str,
    keys: Sequence[tuple[str, str]],
    *,
    chunk_size: int = 500,
) -> None:
    if table not in {"comment_topics", "comment_tags"}:
        raise ImportDataError(f"unsupported label table: {table}")
    grouped: dict[str, list[str]] = defaultdict(list)
    for platform, comment_id in keys:
        grouped[platform].append(comment_id)
    for platform, comment_ids in grouped.items():
        for offset in range(0, len(comment_ids), chunk_size):
            chunk = comment_ids[offset : offset + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"DELETE FROM {table} WHERE platform = %s "
                f"AND comment_id IN ({placeholders})",
                [platform, *chunk],
            )


def portable_path(path: Path, *, base: Path | None = None) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to((base or REPOSITORY_ROOT).resolve()).as_posix()
    except ValueError:
        return resolved.name


def import_payload(
    connection: Any,
    payload: ImportPayload,
    *,
    comments_path: Path,
    targets_path: Path,
    progress_path: Path,
    manage_transaction: bool = True,
    verify: bool = True,
) -> tuple[dict[str, int], dict[str, Any]]:
    started_at = utc_now_naive()
    generated_at = to_mysql_datetime(payload.targets["generated_at"])
    progress_updated_at = to_mysql_datetime(payload.progress["updated_at"])
    first_collected: dict[tuple[str, str], datetime] = {}
    for snapshot in payload.snapshots:
        key = (snapshot["platform"], snapshot["comment_id"])
        collected_at = to_mysql_datetime(snapshot["collected_at"])
        previous = first_collected.get(key)
        first_collected[key] = collected_at if previous is None else min(previous, collected_at)

    try:
        if manage_transaction:
            connection.begin()
        with connection.cursor() as cursor:
            cursor.executemany(
                COLLECTION_UPSERT,
                [
                    (
                        PLATFORM,
                        item["collection_id"],
                        item["name"],
                        item["episode_count"],
                        payload.targets["scope_id"],
                        generated_at,
                    )
                    for item in payload.targets["collections"]
                ],
            )
            cursor.executemany(VIDEO_UPSERT, build_video_rows(payload))
            pruned = prune_removed_target_state(
                cursor,
                payload=payload,
                manifest_generated_at=generated_at,
                progress_updated_at=progress_updated_at,
            )
            incoming_mappings = [
                {"platform": PLATFORM, **item} for item in payload.targets["videos"]
            ]
            existing_mappings = fetch_existing_collection_video_mappings(
                cursor, incoming_mappings
            )
            validate_collection_video_mappings(incoming_mappings, existing_mappings)
            cursor.executemany(
                COLLECTION_VIDEO_UPSERT,
                [
                    (
                        PLATFORM,
                        item["collection_id"],
                        item["video_id"],
                        item["episode"],
                        item.get("card_metric", ""),
                        item["status"],
                        generated_at,
                    )
                    for item in payload.targets["videos"]
                ],
            )
            cursor.executemany(
                PROGRESS_UPSERT,
                [
                    (
                        PLATFORM,
                        payload.progress["scope_id"],
                        video_id,
                        item["status"],
                        item["visible_comment_count"],
                        item["stored_record_count"],
                        item.get("last_batch", ""),
                        to_mysql_datetime(item.get("last_collected_at")),
                        item.get("notes", ""),
                        progress_updated_at,
                    )
                    for video_id, item in sorted(payload.progress["videos"].items())
                ],
            )
            ordered_comments = parent_first(payload.comments)
            existing = fetch_existing_comment_state(cursor, ordered_comments)
            accepted_keys = accepted_current_keys(ordered_comments, existing)

            cursor.executemany(
                COMMENT_UPSERT,
                [
                    (
                        record["platform"],
                        record["comment_id"],
                        record["schema_version"],
                        record["comment_id_kind"],
                        record["video_id"],
                        record["parent_comment_id"],
                        record["root_comment_id"],
                        record["comment_type"],
                        record["author_role"],
                        record["text"],
                        record["like_count"],
                        record["reply_count"],
                        to_mysql_datetime(record["published_at"]),
                        record["published_label"],
                        first_collected[(record["platform"], record["comment_id"])],
                        to_mysql_datetime(record["collected_at"]),
                        record["source_url"],
                        record["collection_batch"],
                    )
                    for record in ordered_comments
                ],
            )

            snapshot_count_before = scalar(
                cursor, "SELECT COUNT(*) AS count FROM comment_snapshots"
            )
            validate_existing_comment_snapshots(cursor, payload.snapshots)
            cursor.executemany(
                SNAPSHOT_INSERT,
                [
                    (
                        record["platform"],
                        record["comment_id"],
                        to_mysql_datetime(record["collected_at"]),
                        record["schema_version"],
                        record["comment_id_kind"],
                        record["video_id"],
                        record["video_title"],
                        record["video_url"],
                        record["parent_comment_id"],
                        record["root_comment_id"],
                        record["comment_type"],
                        record["author_role"],
                        record["text"],
                        record["like_count"],
                        record["reply_count"],
                        to_mysql_datetime(record["published_at"]),
                        record["published_label"],
                        record["source_url"],
                        record["collection_batch"],
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    )
                    for record in payload.snapshots
                ],
            )
            snapshot_count_after = scalar(
                cursor, "SELECT COUNT(*) AS count FROM comment_snapshots"
            )
            inserted_snapshots = snapshot_count_after - snapshot_count_before

            accepted = set(accepted_keys)
            accepted_records = [
                record
                for record in ordered_comments
                if (record["platform"], record["comment_id"]) in accepted
            ]
            delete_current_labels(cursor, "comment_topics", accepted_keys)
            delete_current_labels(cursor, "comment_tags", accepted_keys)
            topic_rows = [
                (record["platform"], record["comment_id"], topic_id)
                for record in accepted_records
                for topic_id in record["topic_ids"]
            ]
            tag_rows = [
                (record["platform"], record["comment_id"], tag)
                for record in accepted_records
                for tag in record["manual_tags"]
            ]
            if topic_rows:
                cursor.executemany(
                    "INSERT INTO comment_topics (platform, comment_id, topic_id) "
                    "VALUES (%s, %s, %s)",
                    topic_rows,
                )
            if tag_rows:
                cursor.executemany(
                    "INSERT INTO comment_tags (platform, comment_id, tag) "
                    "VALUES (%s, %s, %s)",
                    tag_rows,
                )

            completed_at = utc_now_naive()
            cursor.execute(
                """
                INSERT INTO import_runs
                    (source_path, source_sha256, target_manifest_path,
                     target_manifest_sha256, progress_path, progress_sha256,
                     started_at, completed_at, status, raw_comment_count,
                     duplicate_source_count, stored_comment_count,
                     inserted_snapshot_count, collection_count,
                     target_video_count, progress_video_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'success', %s, %s,
                        %s, %s, %s, %s, %s)
                """,
                (
                    portable_path(comments_path, base=comments_path.parent.parent),
                    payload.source_sha256,
                    portable_path(targets_path, base=comments_path.parent.parent),
                    payload.targets_sha256,
                    portable_path(progress_path, base=comments_path.parent.parent),
                    payload.progress_sha256,
                    started_at,
                    completed_at,
                    len(payload.snapshots),
                    payload.duplicate_source_count,
                    len(payload.comments),
                    inserted_snapshots,
                    len(payload.targets["collections"]),
                    len(payload.targets["videos"]),
                    len(payload.progress["videos"]),
                ),
            )
            verification = verify_database(connection) if verify else {}
        if manage_transaction:
            connection.commit()
    except Exception:
        if manage_transaction:
            connection.rollback()
        raise
    return {
        "raw_comment_count": len(payload.snapshots),
        "canonical_comment_count": len(payload.comments),
        "duplicate_source_count": payload.duplicate_source_count,
        "inserted_snapshot_count": inserted_snapshots,
        "accepted_current_count": len(accepted_keys),
        "pruned_collections": pruned["collections"],
        "pruned_collection_videos": pruned["collection_videos"],
        "pruned_progress_rows": pruned["progress"],
    }, verification


def scalar(cursor: Any, query: str, parameters: Iterable[Any] | None = None) -> int:
    cursor.execute(query, parameters)
    row = cursor.fetchone()
    return int(next(iter(row.values())))


def verify_database(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DEFAULT_CHARACTER_SET_NAME AS charset_name, "
            "DEFAULT_COLLATION_NAME AS collation_name "
            "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()"
        )
        metadata = cursor.fetchone()
        if metadata is None:
            raise ImportDataError("the selected MySQL database does not exist")
        cursor.execute(
            "SELECT TABLE_NAME AS table_name, TABLE_COLLATION AS table_collation "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
        )
        table_rows = cursor.fetchall()
        tables = {row["table_name"] for row in table_rows}
        missing = sorted(set(REQUIRED_TABLES) - tables)
        if missing:
            raise ImportDataError(f"missing required MySQL tables: {', '.join(missing)}")
        table_collations = {
            row["table_name"]: row["table_collation"]
            for row in table_rows
            if row["table_name"] in REQUIRED_TABLES
        }
        unexpected_collations = {
            table: collation
            for table, collation in sorted(table_collations.items())
            if collation != EXPECTED_TABLE_COLLATION
        }
        if unexpected_collations:
            details = ", ".join(
                f"{table}={collation}"
                for table, collation in unexpected_collations.items()
            )
            raise ImportDataError(
                f"unexpected table collations (expected {EXPECTED_TABLE_COLLATION}): {details}"
            )

        cursor.execute(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
        )
        actual_columns: dict[str, set[str]] = defaultdict(set)
        for row in cursor.fetchall():
            if row["table_name"] in REQUIRED_TABLES:
                actual_columns[row["table_name"]].add(row["column_name"])
        cursor.execute(
            "SELECT TABLE_NAME AS table_name, INDEX_NAME AS index_name, "
            "COLUMN_NAME AS column_name, SEQ_IN_INDEX AS sequence_number "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND NON_UNIQUE = 0 "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
        )
        unique_columns: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        for row in cursor.fetchall():
            if row["table_name"] in REQUIRED_TABLES:
                unique_columns[(row["table_name"], row["index_name"])].append(
                    (int(row["sequence_number"]), row["column_name"])
                )
        actual_unique_keys: dict[str, set[tuple[str, ...]]] = defaultdict(set)
        for (table, _), columns in unique_columns.items():
            actual_unique_keys[table].add(
                tuple(column for _, column in sorted(columns))
            )
        try:
            validate_schema_contract(actual_columns, actual_unique_keys)
        except SchemaContractError as exc:
            raise ImportDataError(f"MySQL schema contract mismatch: {exc}") from exc

        result: dict[str, Any] = {
            "database_charset": metadata["charset_name"],
            "database_collation": metadata["collation_name"],
            "table_collation": EXPECTED_TABLE_COLLATION,
            "unexpected_table_collations": 0,
        }
        for table in REQUIRED_TABLES:
            result[table] = scalar(cursor, f"SELECT COUNT(*) AS count FROM `{table}`")
        result["root_comments"] = scalar(
            cursor, "SELECT COUNT(*) AS count FROM comments WHERE comment_type = 'root'"
        )
        result["reply_comments"] = scalar(
            cursor, "SELECT COUNT(*) AS count FROM comments WHERE comment_type = 'reply'"
        )
        result["creator_comments"] = scalar(
            cursor, "SELECT COUNT(*) AS count FROM comments WHERE author_role = 'creator'"
        )
        result["orphan_parents"] = scalar(
            cursor,
            """
            SELECT COUNT(*) AS count
            FROM comments child
            LEFT JOIN comments parent
              ON parent.platform = child.platform
             AND parent.comment_id = child.parent_comment_id
            WHERE child.comment_type = 'reply' AND parent.comment_id IS NULL
            """,
        )
        result["orphan_roots"] = scalar(
            cursor,
            """
            SELECT COUNT(*) AS count
            FROM comments child
            LEFT JOIN comments root_comment
              ON root_comment.platform = child.platform
             AND root_comment.comment_id = child.root_comment_id
            WHERE child.comment_type = 'reply' AND root_comment.comment_id IS NULL
            """,
        )
        result["cross_video_relationships"] = scalar(
            cursor,
            """
            SELECT COUNT(*) AS count
            FROM comments child
            JOIN comments parent
              ON parent.platform = child.platform
             AND parent.comment_id = child.parent_comment_id
            JOIN comments root_comment
              ON root_comment.platform = child.platform
             AND root_comment.comment_id = child.root_comment_id
            WHERE child.comment_type = 'reply'
              AND (child.video_id <> parent.video_id
                   OR child.video_id <> root_comment.video_id
                   OR root_comment.comment_type <> 'root')
            """,
        )
        return result


def print_result(title: str, result: Mapping[str, Any]) -> None:
    print(title)
    for key, value in result.items():
        print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import the active CSI OpenBase comment archive into local MySQL."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--database")
    parser.add_argument("--comments", type=Path, default=DEFAULT_COMMENTS)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--progress", type=Path, default=DEFAULT_PROGRESS)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="run read-only counts and relationship checks without creating or importing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        try:
            from admin_app.workspace import load_active_workspace

            workspace = load_active_workspace()
        except Exception as exc:
            raise ImportDataError("an active workspace is required") from exc
        args.comments = args.comments or (workspace.comments_dir / "comments.jsonl")
        args.targets = args.targets or (workspace.comments_dir / "collection-targets.json")
        args.progress = args.progress or (workspace.comments_dir / "collection-progress.json")
        workspace_root = workspace.directory.resolve()
        for label, path in (
            ("comments", args.comments),
            ("targets", args.targets),
            ("progress", args.progress),
        ):
            resolved = path.resolve()
            if not resolved.is_relative_to(workspace_root):
                raise ImportDataError(
                    f"--{label} must stay inside the active workspace: {workspace_root}"
                )
        if args.database and args.database.casefold() != workspace.database_name.casefold():
            raise ImportDataError(
                "--database must match the active workspace database "
                f"({workspace.database_name})"
            )
        args.database = workspace.database_name
        if not 1 <= args.port <= 65535:
            raise ImportDataError("port must be between 1 and 65535")
        validate_identifier(args.database, label="database name")
        password = resolve_password()
        if args.verify_only:
            connection = connect_mysql(
                host=args.host,
                port=args.port,
                user=args.user,
                password=password,
                database=args.database,
            )
            try:
                assert_existing_workspace_identity(
                    connection,
                    workspace_slug=workspace.slug,
                    platform=workspace.platform,
                )
                print_result("MySQL verification", verify_database(connection))
            finally:
                connection.close()
            return 0

        with archive_lock(args.comments.resolve().parent / ".archive.lock"):
            payload = load_import_payload(args.comments, args.targets, args.progress)
            create_database_if_missing(
                host=args.host,
                port=args.port,
                user=args.user,
                password=password,
                database=args.database,
            )
            connection = connect_mysql(
                host=args.host,
                port=args.port,
                user=args.user,
                password=password,
                database=args.database,
            )
            try:
                assert_existing_workspace_identity(
                    connection,
                    workspace_slug=workspace.slug,
                    platform=workspace.platform,
                )
                apply_schema(connection, DEFAULT_SCHEMA)
                bind_workspace_identity(
                    connection,
                    workspace_slug=workspace.slug,
                    platform=workspace.platform,
                )
                import_stats, verification = import_payload(
                    connection,
                    payload,
                    comments_path=args.comments,
                    targets_path=args.targets,
                    progress_path=args.progress,
                )
            finally:
                connection.close()
        print_result("MySQL import", import_stats)
        print_result("MySQL verification", verification)
        return 0
    except (CommentDataError, ImportDataError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if pymysql is not None and isinstance(exc, pymysql.MySQLError):
            print(f"MySQL error: {exc}", file=sys.stderr)
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
