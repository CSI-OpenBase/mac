"""Small SQLite index for the desktop archive workflow.

Raw downloads and immutable JSON/JSONL snapshots remain the source of truth.
SQLite only indexes work, jobs, and local UI state so the desktop build has no
external database dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .local_config import LOCAL_PREFERENCES_META_KEY


JOB_KINDS = frozenset(
    {"authorize", "export", "sync_videos", "comment_count", "comments"}
)
JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "partial", "blocked", "failed", "interrupted"}
)
CLEAR_DATA_SCOPES = frozenset({"exports", "comments", "all"})
CLEAR_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
VIDEO_ID_RE = re.compile(r"^[0-9]{8,32}$")
GROUP_ID_RE = re.compile(r"^(?:manual|platform):[a-z0-9][a-z0-9._:-]{0,127}$")
PLATFORM_GROUP_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
MAX_GROUP_NAME_LENGTH = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _group_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("分组名称不能为空")
    if len(name) > MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"分组名称不能超过 {MAX_GROUP_NAME_LENGTH} 个字符")
    return name


def _platform_group_id(platform: str, source_key: str) -> str:
    digest = hashlib.sha256(f"{platform}:{source_key}".encode("utf-8")).hexdigest()[:24]
    return f"platform:{platform}:{digest}"


class ActiveCommentJobError(ValueError):
    """Raised when one video already has an active comment-related job."""


class JobStateConflictError(RuntimeError):
    """Raised when a worker tries to overwrite a job it no longer owns."""


class ActiveLocalJobsError(RuntimeError):
    """Raised when maintenance would overlap queued or running work."""


class LocalStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    video_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_archive_jobs_created
                    ON archive_jobs(created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS archive_videos (
                    video_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT 'douyin',
                    title TEXT NOT NULL,
                    video_url TEXT NOT NULL,
                    cover_path TEXT,
                    manifest_path TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_comment_export_at TEXT,
                    visible_comment_count INTEGER,
                    last_comment_count_at TEXT,
                    comment_count_delta INTEGER,
                    comment_count INTEGER NOT NULL DEFAULT 0,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_archive_videos_last_seen
                    ON archive_videos(last_seen_at DESC, video_id DESC);
                CREATE TABLE IF NOT EXISTS archive_groups (
                    group_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT 'douyin',
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(source IN ('platform', 'manual'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_archive_groups_platform_source
                    ON archive_groups(platform, source_key)
                    WHERE source = 'platform';
                CREATE INDEX IF NOT EXISTS ix_archive_groups_name
                    ON archive_groups(source, name, group_id);
                CREATE TABLE IF NOT EXISTS archive_video_groups (
                    group_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(group_id, video_id),
                    FOREIGN KEY(group_id) REFERENCES archive_groups(group_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(video_id) REFERENCES archive_videos(video_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_archive_video_groups_video
                    ON archive_video_groups(video_id, group_id);
                CREATE TABLE IF NOT EXISTS archive_clear_operations (
                    operation_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    committed_at TEXT NOT NULL
                );
                """
            )
            video_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(archive_videos)")
            }
            if "visible_comment_count" not in video_columns:
                connection.execute(
                    """
                    ALTER TABLE archive_videos
                    ADD COLUMN visible_comment_count INTEGER
                    """
                )
            if "last_comment_count_at" not in video_columns:
                connection.execute(
                    """
                    ALTER TABLE archive_videos
                    ADD COLUMN last_comment_count_at TEXT
                    """
                )
            if "comment_count_delta" not in video_columns:
                connection.execute(
                    """
                    ALTER TABLE archive_videos
                    ADD COLUMN comment_count_delta INTEGER
                    """
                )
            connection.execute("DROP INDEX IF EXISTS ux_active_video_comment_job")
            connection.execute(
                """
                CREATE UNIQUE INDEX ux_active_video_comment_job
                    ON archive_jobs(video_id)
                    WHERE kind IN ('comments', 'comment_count')
                      AND status IN ('queued', 'running')
                """
            )

    @contextmanager
    def exclusive_maintenance(self) -> Iterator[None]:
        """Block job creation while maintenance verifies an idle workspace."""

        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                      FROM archive_jobs
                     WHERE status IN ('queued', 'running')
                    """
                ).fetchone()
            if int(row["count"]):
                raise ActiveLocalJobsError(
                    "当前仍有任务等待或运行，请在任务完成后再清空数据"
                )
            yield

    def clear_records(self, scope: str, *, operation_id: str) -> None:
        if scope not in CLEAR_DATA_SCOPES:
            raise ValueError("不支持的数据清理范围")
        if not CLEAR_OPERATION_ID_RE.fullmatch(operation_id):
            raise ValueError("invalid clear operation id")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                  FROM archive_jobs
                 WHERE status IN ('queued', 'running')
                """
            ).fetchone()
            if int(row["count"]):
                raise ActiveLocalJobsError(
                    "当前仍有任务等待或运行，请在任务完成后再清空数据"
                )
            if scope == "exports":
                connection.execute("DELETE FROM archive_jobs WHERE kind = 'export'")
                connection.execute("DELETE FROM app_meta WHERE key = 'last_export'")
            elif scope == "comments":
                connection.execute("DELETE FROM archive_jobs WHERE kind = 'comments'")
                connection.execute(
                    """
                    UPDATE archive_videos
                       SET comment_count = 0,
                           last_comment_export_at = NULL,
                           updated_at = ?
                    """,
                    (utc_now(),),
                )
            else:
                connection.execute("DELETE FROM archive_jobs")
                connection.execute("DELETE FROM archive_videos")
                connection.execute("DELETE FROM archive_groups")
                connection.execute(
                    "DELETE FROM app_meta WHERE key <> ?",
                    (LOCAL_PREFERENCES_META_KEY,),
                )
                connection.execute(
                    "DELETE FROM sqlite_sequence WHERE name = 'archive_jobs'"
                )
            connection.execute(
                """
                INSERT INTO archive_clear_operations(
                    operation_id, scope, committed_at
                ) VALUES (?, ?, ?)
                """,
                (operation_id, scope, utc_now()),
            )

    def committed_clear_operations(self) -> dict[str, str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id, scope FROM archive_clear_operations"
            ).fetchall()
        return {str(row["operation_id"]): str(row["scope"]) for row in rows}

    def finish_clear_operation(self, operation_id: str) -> None:
        if not CLEAR_OPERATION_ID_RE.fullmatch(operation_id):
            raise ValueError("invalid clear operation id")
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM archive_clear_operations WHERE operation_id = ?",
                (operation_id,),
            )

    def interrupt_active_jobs(self) -> int:
        """Mark abandoned work after the caller has acquired the workspace lease."""

        with self._lock, self._connect() as connection:
            return connection.execute(
                """
                UPDATE archive_jobs
                   SET status = 'interrupted', finished_at = ?,
                       message = '应用关闭，任务已中断'
                 WHERE status IN ('queued', 'running')
                """,
                (utc_now(),),
            ).rowcount

    def set_meta(self, key: str, value: Any) -> None:
        if not key.strip():
            raise ValueError("metadata key cannot be empty")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO app_meta(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, _json(value), utc_now()),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
        return _decode(row["value_json"], default) if row else default

    def create_job(
        self,
        kind: str,
        *,
        video_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in JOB_KINDS:
            raise ValueError(f"unsupported local job kind: {kind}")
        normalized_video_id = video_id.strip() if video_id else None
        if kind in {"comments", "comment_count"} and not normalized_video_id:
            raise ValueError("comment jobs require a video_id")
        if normalized_video_id and not VIDEO_ID_RE.fullmatch(normalized_video_id):
            raise ValueError("video_id must contain 8-32 digits")
        now = utc_now()
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO archive_jobs(
                        kind, status, video_id, payload_json, created_at
                    ) VALUES (?, 'queued', ?, ?, ?)
                    """,
                    (kind, normalized_video_id, _json(dict(payload or {})), now),
                )
                job_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if kind in {"comments", "comment_count"}:
                raise ActiveCommentJobError(
                    "该视频已有等待中或正在运行的评论任务"
                ) from exc
            raise
        job = self.get_job(job_id)
        assert job is not None
        return job

    def update_job(
        self,
        job_id: int,
        status: str,
        *,
        message: str | None = None,
        result: Mapping[str, Any] | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise ValueError(f"unsupported local job status: {status}")
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = (
            now
            if status in {"succeeded", "partial", "blocked", "failed", "interrupted"}
            else None
        )
        assignments = ["status = ?", "message = ?", "result_json = ?"]
        parameters: list[Any] = [status, message, _json(dict(result)) if result else None]
        if started_at:
            assignments.append("started_at = COALESCE(started_at, ?)")
            parameters.append(started_at)
        if finished_at:
            assignments.append("finished_at = ?")
            parameters.append(finished_at)
        parameters.append(job_id)
        condition = "id = ?"
        if expected_status is not None:
            if expected_status not in JOB_STATUSES:
                raise ValueError(f"unsupported expected job status: {expected_status}")
            condition += " AND status = ?"
            parameters.append(expected_status)
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                f"UPDATE archive_jobs SET {', '.join(assignments)} WHERE {condition}",
                parameters,
            ).rowcount
        if changed != 1:
            if self.get_job(job_id) is None:
                raise KeyError(f"local job {job_id} does not exist")
            raise JobStateConflictError(
                f"local job {job_id} is no longer {expected_status}"
            )
        job = self.get_job(job_id)
        assert job is not None
        return job

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = _decode(value.pop("payload_json", None), {})
        value["result"] = _decode(value.pop("result_json", None), None)
        return value

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 200)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archive_jobs ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self._job(row) for row in rows]

    def active_job_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM archive_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row["count"])

    def upsert_videos(self, records: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        now = utc_now()
        with self._lock, self._connect() as connection:
            for raw in records:
                record = dict(raw)
                video_id = str(record.get("video_id") or "").strip()
                title = str(record.get("title") or record.get("description") or "").strip()
                video_url = str(record.get("video_url") or "").strip()
                manifest_path = str(record.get("manifest_path") or "").strip()
                first_seen_at = str(record.get("first_seen_at") or now)
                last_seen_at = str(record.get("last_seen_at") or now)
                raw_visible_comment_count = record.get("visible_comment_count")
                visible_comment_count = None
                if raw_visible_comment_count not in (None, ""):
                    if isinstance(raw_visible_comment_count, bool):
                        raise ValueError("visible_comment_count must be a non-negative integer")
                    try:
                        visible_comment_count = int(raw_visible_comment_count)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "visible_comment_count must be a non-negative integer"
                        ) from exc
                    if visible_comment_count < 0:
                        raise ValueError(
                            "visible_comment_count must be a non-negative integer"
                        )
                if not VIDEO_ID_RE.fullmatch(video_id):
                    raise ValueError("video_id must contain 8-32 digits")
                if not title or not video_url or not manifest_path:
                    raise ValueError("video index record is missing required fields")
                connection.execute(
                    """
                    INSERT INTO archive_videos(
                        video_id, platform, title, video_url, cover_path,
                        manifest_path, first_seen_at, last_seen_at,
                        visible_comment_count, last_comment_count_at,
                        comment_count_delta, record_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        title = excluded.title,
                        video_url = excluded.video_url,
                        cover_path = COALESCE(excluded.cover_path, archive_videos.cover_path),
                        manifest_path = excluded.manifest_path,
                        first_seen_at = MIN(archive_videos.first_seen_at, excluded.first_seen_at),
                        last_seen_at = MAX(archive_videos.last_seen_at, excluded.last_seen_at),
                        comment_count_delta = CASE
                            WHEN ? AND archive_videos.visible_comment_count IS NOT NULL
                                THEN excluded.visible_comment_count - archive_videos.visible_comment_count
                            WHEN ? THEN NULL
                            ELSE archive_videos.comment_count_delta
                        END,
                        last_comment_count_at = CASE
                            WHEN ? THEN excluded.last_comment_count_at
                            ELSE archive_videos.last_comment_count_at
                        END,
                        visible_comment_count = CASE
                            WHEN ? THEN excluded.visible_comment_count
                            ELSE archive_videos.visible_comment_count
                        END,
                        record_json = excluded.record_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        video_id,
                        str(record.get("platform") or "douyin"),
                        title,
                        video_url,
                        str(record.get("cover_path") or "") or None,
                        manifest_path,
                        first_seen_at,
                        last_seen_at,
                        visible_comment_count,
                        last_seen_at if visible_comment_count is not None else None,
                        _json(record),
                        now,
                        visible_comment_count is not None,
                        visible_comment_count is not None,
                        visible_comment_count is not None,
                        visible_comment_count is not None,
                    ),
                )
                if record.get("platform_groups_observed") is True:
                    self._replace_platform_groups(
                        connection,
                        video_id=video_id,
                        platform=str(record.get("platform") or "douyin"),
                        groups=record.get("platform_groups") or (),
                        now=now,
                    )
                count += 1
        return count

    @staticmethod
    def _replace_platform_groups(
        connection: sqlite3.Connection,
        *,
        video_id: str,
        platform: str,
        groups: Iterable[Mapping[str, Any]],
        now: str,
    ) -> None:
        normalized: list[tuple[str, str]] = []
        for raw in groups:
            if not isinstance(raw, Mapping):
                raise ValueError("平台栏目数据必须是对象")
            source_key = str(raw.get("id") or "").strip()
            if not PLATFORM_GROUP_KEY_RE.fullmatch(source_key):
                raise ValueError("平台栏目 ID 格式无效")
            normalized.append((source_key, _group_name(raw.get("name"))))

        connection.execute(
            """
            DELETE FROM archive_video_groups
             WHERE video_id = ?
               AND group_id IN (
                    SELECT group_id FROM archive_groups
                     WHERE source = 'platform' AND platform = ?
               )
            """,
            (video_id, platform),
        )
        for source_key, name in dict(normalized).items():
            group_id = _platform_group_id(platform, source_key)
            connection.execute(
                """
                INSERT INTO archive_groups(
                    group_id, platform, name, source, source_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'platform', ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (group_id, platform, name, source_key, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO archive_video_groups(
                    group_id, video_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (group_id, video_id, now),
            )

    @staticmethod
    def _video(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["record"] = _decode(value.pop("record_json", None), {})
        value.setdefault("groups", [])
        return value

    @staticmethod
    def _attach_groups(
        connection: sqlite3.Connection, videos: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not videos:
            return videos
        by_id = {str(video["video_id"]): video for video in videos}
        placeholders = ",".join("?" for _ in by_id)
        rows = connection.execute(
            f"""
            SELECT vg.video_id, g.group_id, g.name, g.source, g.source_key
              FROM archive_video_groups vg
              JOIN archive_groups g ON g.group_id = vg.group_id
             WHERE vg.video_id IN ({placeholders})
             ORDER BY g.source, g.name, g.group_id
            """,
            tuple(by_id),
        ).fetchall()
        for row in rows:
            by_id[str(row["video_id"])]["groups"].append(
                {
                    "group_id": str(row["group_id"]),
                    "name": str(row["name"]),
                    "source": str(row["source"]),
                    "source_key": row["source_key"],
                }
            )
        return videos

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            videos = [self._video(row)] if row else []
            self._attach_groups(connection, videos)
        return videos[0] if videos else None

    def video_page_number(self, video_id: str, *, page_size: int = 30) -> int | None:
        if not VIDEO_ID_RE.fullmatch(video_id):
            return None
        bounded_page_size = min(max(int(page_size), 1), 100)
        with self._lock, self._connect() as connection:
            target = connection.execute(
                "SELECT last_seen_at FROM archive_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if target is None:
                return None
            preceding = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                      FROM archive_videos
                     WHERE last_seen_at > ?
                        OR (last_seen_at = ? AND video_id > ?)
                    """,
                    (target["last_seen_at"], target["last_seen_at"], video_id),
                ).fetchone()["count"]
            )
        return preceding // bounded_page_size + 1

    def list_videos(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        bounded = min(max(int(limit), 1), 100_000)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM archive_videos
                ORDER BY last_seen_at DESC, video_id DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
            videos = [self._video(row) for row in rows]
            self._attach_groups(connection, videos)
        return videos

    def video_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM archive_videos"
            ).fetchone()
        return int(row["count"])

    def list_video_page(
        self, *, page: int = 1, page_size: int = 30, group_id: str | None = None
    ) -> dict[str, Any]:
        requested_page = max(int(page), 1)
        bounded_page_size = min(max(int(page_size), 1), 100)
        parameters: list[Any] = []
        where = ""
        if group_id == "ungrouped":
            where = (
                "WHERE NOT EXISTS (SELECT 1 FROM archive_video_groups vg "
                "WHERE vg.video_id = archive_videos.video_id)"
            )
        elif group_id:
            if not GROUP_ID_RE.fullmatch(group_id):
                raise ValueError("分组 ID 格式无效")
            where = (
                "WHERE EXISTS (SELECT 1 FROM archive_video_groups vg "
                "WHERE vg.video_id = archive_videos.video_id AND vg.group_id = ?)"
            )
            parameters.append(group_id)
        with self._lock, self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM archive_videos {where}",
                    parameters,
                ).fetchone()["count"]
            )
            pages = max(1, (total + bounded_page_size - 1) // bounded_page_size)
            current_page = min(requested_page, pages)
            offset = (current_page - 1) * bounded_page_size
            rows = connection.execute(
                f"""
                SELECT * FROM archive_videos
                {where}
                ORDER BY last_seen_at DESC, video_id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, bounded_page_size, offset),
            ).fetchall()
            videos = [self._video(row) for row in rows]
            self._attach_groups(connection, videos)
        return {
            "items": videos,
            "total": total,
            "page": current_page,
            "page_size": bounded_page_size,
            "pages": pages,
        }

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT g.group_id, g.platform, g.name, g.source, g.source_key,
                       g.created_at, g.updated_at, COUNT(vg.video_id) AS video_count
                  FROM archive_groups g
                  LEFT JOIN archive_video_groups vg ON vg.group_id = g.group_id
                 GROUP BY g.group_id
                 ORDER BY CASE g.source WHEN 'platform' THEN 0 ELSE 1 END,
                          g.name, g.group_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        if not GROUP_ID_RE.fullmatch(group_id):
            return None
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT g.group_id, g.platform, g.name, g.source, g.source_key,
                       g.created_at, g.updated_at, COUNT(vg.video_id) AS video_count
                  FROM archive_groups g
                  LEFT JOIN archive_video_groups vg ON vg.group_id = g.group_id
                 WHERE g.group_id = ?
                 GROUP BY g.group_id
                """,
                (group_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_group(self, name: str) -> dict[str, Any]:
        now = utc_now()
        group_id = f"manual:{uuid.uuid4().hex}"
        normalized_name = _group_name(name)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO archive_groups(
                    group_id, platform, name, source, source_key,
                    created_at, updated_at
                ) VALUES (?, 'douyin', ?, 'manual', NULL, ?, ?)
                """,
                (group_id, normalized_name, now, now),
            )
        group = self.get_group(group_id)
        assert group is not None
        return group

    def rename_group(self, group_id: str, name: str) -> dict[str, Any]:
        if not GROUP_ID_RE.fullmatch(group_id):
            raise ValueError("分组 ID 格式无效")
        normalized_name = _group_name(name)
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE archive_groups SET name = ?, updated_at = ?
                 WHERE group_id = ? AND source = 'manual'
                """,
                (normalized_name, utc_now(), group_id),
            ).rowcount
        if changed != 1:
            raise ValueError("只能重命名用户创建的分组")
        group = self.get_group(group_id)
        assert group is not None
        return group

    def delete_group(self, group_id: str) -> None:
        if not GROUP_ID_RE.fullmatch(group_id):
            raise ValueError("分组 ID 格式无效")
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "DELETE FROM archive_groups WHERE group_id = ? AND source = 'manual'",
                (group_id,),
            ).rowcount
        if changed != 1:
            raise ValueError("只能删除用户创建的分组")

    def _change_group_videos(
        self, group_id: str, video_ids: Iterable[str], *, add: bool
    ) -> int:
        if not GROUP_ID_RE.fullmatch(group_id):
            raise ValueError("分组 ID 格式无效")
        normalized_ids = tuple(dict.fromkeys(str(value).strip() for value in video_ids))
        if not normalized_ids:
            raise ValueError("请选择至少一个视频")
        if len(normalized_ids) > 100:
            raise ValueError("一次最多管理 100 个视频")
        if any(not VIDEO_ID_RE.fullmatch(value) for value in normalized_ids):
            raise ValueError("视频 ID 格式无效")
        with self._lock, self._connect() as connection:
            group = connection.execute(
                "SELECT source FROM archive_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
            if group is None or group["source"] != "manual":
                raise ValueError("只能管理用户创建的分组")
            placeholders = ",".join("?" for _ in normalized_ids)
            existing = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM archive_videos "
                    f"WHERE video_id IN ({placeholders})",
                    normalized_ids,
                ).fetchone()["count"]
            )
            if existing != len(normalized_ids):
                raise ValueError("选择中包含不存在的视频")
            if add:
                changed = 0
                now = utc_now()
                for video_id in normalized_ids:
                    changed += connection.execute(
                        """
                        INSERT OR IGNORE INTO archive_video_groups(
                            group_id, video_id, created_at
                        ) VALUES (?, ?, ?)
                        """,
                        (group_id, video_id, now),
                    ).rowcount
            else:
                changed = connection.execute(
                    f"""
                    DELETE FROM archive_video_groups
                     WHERE group_id = ? AND video_id IN ({placeholders})
                    """,
                    (group_id, *normalized_ids),
                ).rowcount
        return changed

    def add_videos_to_group(self, group_id: str, video_ids: Iterable[str]) -> int:
        return self._change_group_videos(group_id, video_ids, add=True)

    def remove_videos_from_group(self, group_id: str, video_ids: Iterable[str]) -> int:
        return self._change_group_videos(group_id, video_ids, add=False)

    def prune_empty_platform_groups(self, *, platform: str = "douyin") -> int:
        with self._lock, self._connect() as connection:
            return connection.execute(
                """
                DELETE FROM archive_groups
                 WHERE source = 'platform' AND platform = ?
                   AND NOT EXISTS (
                       SELECT 1 FROM archive_video_groups vg
                        WHERE vg.group_id = archive_groups.group_id
                   )
                """,
                (platform,),
            ).rowcount

    def record_comment_export(
        self, video_id: str, *, count: int, exported_at: str | None = None
    ) -> None:
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE archive_videos
                   SET comment_count = ?, last_comment_export_at = ?, updated_at = ?
                 WHERE video_id = ?
                """,
                (max(0, int(count)), exported_at or utc_now(), utc_now(), video_id),
            ).rowcount
        if changed != 1:
            raise KeyError(f"video {video_id} does not exist")

    def record_visible_comment_count(
        self, video_id: str, *, count: int, checked_at: str | None = None
    ) -> dict[str, Any]:
        if isinstance(count, bool):
            raise ValueError("comment count must be a non-negative integer")
        try:
            normalized_count = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError("comment count must be a non-negative integer") from exc
        if normalized_count < 0:
            raise ValueError("comment count must be a non-negative integer")
        observed_at = checked_at or utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT visible_comment_count FROM archive_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"video {video_id} does not exist")
            previous_count = row["visible_comment_count"]
            delta = (
                normalized_count - int(previous_count)
                if previous_count is not None
                else None
            )
            connection.execute(
                """
                UPDATE archive_videos
                   SET visible_comment_count = ?, comment_count_delta = ?,
                       last_comment_count_at = ?, updated_at = ?
                 WHERE video_id = ?
                """,
                (normalized_count, delta, observed_at, utc_now(), video_id),
            )
        return {
            "video_id": video_id,
            "previous_count": previous_count,
            "current_count": normalized_count,
            "delta": delta,
            "checked_at": observed_at,
        }
