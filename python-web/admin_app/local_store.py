"""Small SQLite index for the desktop archive workflow.

Raw downloads and immutable JSON/JSONL snapshots remain the source of truth.
SQLite only indexes work, jobs, and local UI state so the desktop build has no
external database dependency.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


JOB_KINDS = frozenset({"authorize", "export", "sync_videos", "comments"})
JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "partial", "blocked", "failed", "interrupted"}
)
CLEAR_DATA_SCOPES = frozenset({"exports", "comments", "all"})
CLEAR_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
VIDEO_ID_RE = re.compile(r"^[0-9]{8,32}$")


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


class ActiveCommentJobError(ValueError):
    """Raised when the operator double-submits one video's comment export."""


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
                CREATE UNIQUE INDEX IF NOT EXISTS ux_active_video_comment_job
                    ON archive_jobs(video_id)
                    WHERE kind = 'comments'
                      AND status IN ('queued', 'running');
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
                    comment_count INTEGER NOT NULL DEFAULT 0,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_archive_videos_last_seen
                    ON archive_videos(last_seen_at DESC, video_id DESC);
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
                connection.execute("DELETE FROM app_meta")
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
        if kind == "comments" and not normalized_video_id:
            raise ValueError("comments jobs require a video_id")
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
            if kind == "comments":
                raise ActiveCommentJobError(
                    "该视频已有等待中或正在运行的评论导出任务"
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
                        visible_comment_count, record_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        title = excluded.title,
                        video_url = excluded.video_url,
                        cover_path = COALESCE(excluded.cover_path, archive_videos.cover_path),
                        manifest_path = excluded.manifest_path,
                        first_seen_at = MIN(archive_videos.first_seen_at, excluded.first_seen_at),
                        last_seen_at = MAX(archive_videos.last_seen_at, excluded.last_seen_at),
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
                        _json(record),
                        now,
                        visible_comment_count is not None,
                    ),
                )
                count += 1
        return count

    @staticmethod
    def _video(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["record"] = _decode(value.pop("record_json", None), {})
        return value

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return self._video(row) if row else None

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
        return [self._video(row) for row in rows]

    def list_video_page(
        self, *, page: int = 1, page_size: int = 30
    ) -> dict[str, Any]:
        requested_page = max(int(page), 1)
        bounded_page_size = min(max(int(page_size), 1), 100)
        with self._lock, self._connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM archive_videos"
                ).fetchone()["count"]
            )
            pages = max(1, (total + bounded_page_size - 1) // bounded_page_size)
            current_page = min(requested_page, pages)
            offset = (current_page - 1) * bounded_page_size
            rows = connection.execute(
                """
                SELECT * FROM archive_videos
                ORDER BY last_seen_at DESC, video_id DESC
                LIMIT ? OFFSET ?
                """,
                (bounded_page_size, offset),
            ).fetchall()
        return {
            "items": [self._video(row) for row in rows],
            "total": total,
            "page": current_page,
            "page_size": bounded_page_size,
            "pages": pages,
        }

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
