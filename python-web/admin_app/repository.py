"""Bound-query data access for the local comment administration app."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, insert, or_, select, text, update
from sqlalchemy.engine import Connection, Engine

from .database import normalize_utc, utc_now
from .models import (
    JOB_STATUSES,
    JOB_TYPES,
    SCHEDULE_TYPES,
    CollectionJob,
    CollectionSchedule,
)


_UNSET = object()
_COMMENT_TYPES = frozenset({"root", "reply"})
_AUTHOR_ROLES = frozenset({"viewer", "creator"})
_TARGET_STATUSES = frozenset({"pending", "partial", "complete", "blocked"})
_JOB_TRANSITIONS = {
    "queued": {"running", "blocked", "cancelled"},
    "running": {"succeeded", "failed", "blocked", "cancelled"},
    "failed": {"queued", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "succeeded": set(),
    "cancelled": set(),
}


def _pagination(page: int, page_size: int, *, maximum: int = 200) -> tuple[int, int]:
    if isinstance(page, bool) or page < 1:
        raise ValueError("page must be at least 1")
    if isinstance(page_size, bool) or not 1 <= page_size <= maximum:
        raise ValueError(f"page_size must be between 1 and {maximum}")
    return page_size, (page - 1) * page_size


def _page_result(
    items: list[dict[str, Any]], total: int, page: int, page_size: int
) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
    }


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in rows]


def _require_choice(value: str, choices: frozenset[str], *, field: str) -> str:
    if value not in choices:
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class Repository:
    """Short-lived connection facade around one shared SQLAlchemy engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def dashboard_stats(self, *, platform: str = "douyin") -> dict[str, Any]:
        query = text(
            """
            SELECT
              (SELECT COUNT(*) FROM collections WHERE platform = :platform)
                AS collection_count,
              (SELECT COUNT(*) FROM videos WHERE platform = :platform)
                AS video_count,
              (SELECT COUNT(*) FROM collection_videos
                 WHERE platform = :platform) AS target_video_count,
              (SELECT COUNT(*) FROM collection_videos
                 WHERE platform = :platform AND target_status = 'complete')
                AS complete_video_count,
              (SELECT COUNT(*) FROM collection_videos
                 WHERE platform = :platform AND target_status = 'partial')
                AS partial_video_count,
              (SELECT COUNT(*) FROM collection_videos
                 WHERE platform = :platform AND target_status = 'pending')
                AS pending_video_count,
              (SELECT COUNT(*) FROM collection_videos
                 WHERE platform = :platform AND target_status = 'blocked')
                AS blocked_video_count,
              (SELECT COUNT(*) FROM comments WHERE platform = :platform)
                AS comment_count,
              (SELECT COUNT(*) FROM comments
                 WHERE platform = :platform AND comment_type = 'root')
                AS root_comment_count,
              (SELECT COUNT(*) FROM comments
                 WHERE platform = :platform AND comment_type = 'reply')
                AS reply_comment_count,
              (SELECT COUNT(*) FROM comments
                 WHERE platform = :platform AND author_role = 'creator')
                AS creator_comment_count,
              (SELECT COUNT(*) FROM comments
                 WHERE platform = :platform AND author_role = 'viewer')
                AS viewer_comment_count,
              (SELECT COUNT(*) FROM comment_snapshots WHERE platform = :platform)
                AS snapshot_count,
              (SELECT COUNT(*) FROM collection_jobs WHERE status = 'queued')
                AS queued_job_count,
              (SELECT COUNT(*) FROM collection_jobs WHERE status = 'running')
                AS running_job_count,
              (SELECT COUNT(*) FROM collection_jobs WHERE status = 'failed')
                AS failed_job_count,
              (SELECT MAX(completed_at) FROM import_runs WHERE status = 'success')
                AS last_import_at,
              (SELECT MIN(scheduled_for) FROM collection_jobs
                 WHERE status = 'queued') AS next_job_at,
              (SELECT MIN(next_run_at) FROM collection_schedules
                 WHERE enabled = 1 AND next_run_at IS NOT NULL) AS next_schedule_at
            """
        )
        with self.engine.connect() as connection:
            result = _row(connection.execute(query, {"platform": platform}).mappings().one())
        assert result is not None
        count_fields = [key for key in result if key.endswith("_count")]
        for key in count_fields:
            result[key] = int(result[key] or 0)
        target_count = result["target_video_count"]
        result["coverage_ratio"] = (
            result["complete_video_count"] / target_count if target_count else 0.0
        )
        return result

    def list_collections(
        self, *, platform: str = "douyin"
    ) -> list[dict[str, Any]]:
        """Return collection metadata and target-status totals."""

        with self.engine.connect() as connection:
            items = _rows(
                connection.execute(
                    text(
                        """
                        SELECT c.platform, c.collection_id, c.name,
                               c.declared_episode_count AS episode_count,
                               COUNT(cv.video_id) AS target_count,
                               COALESCE(SUM(cv.target_status = 'complete'), 0)
                                 AS complete_count,
                               COALESCE(SUM(cv.target_status = 'pending'), 0)
                                 AS pending_count,
                               COALESCE(SUM(cv.target_status = 'partial'), 0)
                                 AS partial_count,
                               COALESCE(SUM(cv.target_status = 'blocked'), 0)
                                 AS blocked_count
                        FROM collections c
                        LEFT JOIN collection_videos cv
                          ON cv.platform = c.platform
                         AND cv.collection_id = c.collection_id
                        WHERE c.platform = :platform
                        GROUP BY c.platform, c.collection_id, c.name,
                                 c.declared_episode_count
                        ORDER BY c.name, c.collection_id
                        """
                    ),
                    {"platform": platform},
                ).mappings().all()
            )
        for item in items:
            for key in (
                "episode_count",
                "target_count",
                "complete_count",
                "pending_count",
                "partial_count",
                "blocked_count",
            ):
                item[key] = int(item[key] or 0)
        return items

    def list_videos(
        self,
        *,
        platform: str = "douyin",
        status: str | None = None,
        collection_id: str | None = None,
        query: str | None = None,
        sort: str = "priority",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        limit, offset = _pagination(page, page_size, maximum=100)
        clauses = ["cv.platform = :platform"]
        parameters: dict[str, Any] = {"platform": platform}
        if status:
            _require_choice(status, _TARGET_STATUSES, field="video status")
            clauses.append("cv.target_status = :status")
            parameters["status"] = status
        if collection_id:
            clauses.append("cv.collection_id = :collection_id")
            parameters["collection_id"] = collection_id
        cleaned_query = _clean_optional_text(query)
        if cleaned_query:
            clauses.append("(v.title LIKE :query OR v.video_id = :exact_query)")
            parameters["query"] = f"%{cleaned_query}%"
            parameters["exact_query"] = cleaned_query
        where_sql = " AND ".join(clauses)

        order_by = {
            "priority": "CASE cv.target_status WHEN 'partial' THEN 0 "
            "WHEN 'pending' THEN 1 WHEN 'blocked' THEN 2 ELSE 3 END, "
            "c.name, cv.episode IS NULL, cv.episode, v.video_id",
            "episode": "c.name, cv.episode IS NULL, cv.episode, v.video_id",
            "comments_desc": "COALESCE(cs.comment_count, 0) DESC, c.name, "
            "cv.episode IS NULL, cv.episode, v.video_id",
            "updated_desc": "ps.last_collected_at IS NULL, "
            "ps.last_collected_at DESC, c.name, cv.episode, v.video_id",
        }.get(sort)
        if order_by is None:
            raise ValueError(f"invalid video sort: {sort!r}")

        count_query = text(
            f"""
            SELECT COUNT(*) AS total
            FROM collection_videos cv
            JOIN videos v
              ON v.platform = cv.platform AND v.video_id = cv.video_id
            WHERE {where_sql}
            """
        )
        data_query = text(
            f"""
            SELECT cv.platform, cv.collection_id, c.name AS collection_name,
                   cv.episode, cv.card_metric, cv.target_status AS status,
                   v.video_id, v.title, v.video_url, v.first_seen_at,
                   v.last_seen_at,
                   COALESCE(cs.comment_count, 0) AS comment_count,
                   COALESCE(cs.root_comment_count, 0) AS root_comment_count,
                   COALESCE(cs.reply_comment_count, 0) AS reply_comment_count,
                   COALESCE(cs.creator_comment_count, 0) AS creator_comment_count,
                   ps.visible_comment_count, ps.stored_record_count,
                   ps.last_collected_at, ps.last_batch
            FROM collection_videos cv
            JOIN collections c
              ON c.platform = cv.platform AND c.collection_id = cv.collection_id
            JOIN videos v
              ON v.platform = cv.platform AND v.video_id = cv.video_id
            LEFT JOIN (
                SELECT platform, video_id, COUNT(*) AS comment_count,
                       SUM(comment_type = 'root') AS root_comment_count,
                       SUM(comment_type = 'reply') AS reply_comment_count,
                       SUM(author_role = 'creator') AS creator_comment_count
                FROM comments
                GROUP BY platform, video_id
            ) cs ON cs.platform = v.platform AND cs.video_id = v.video_id
            LEFT JOIN (
                SELECT platform, video_id,
                       MAX(visible_comment_count) AS visible_comment_count,
                       MAX(stored_record_count) AS stored_record_count,
                       MAX(last_collected_at) AS last_collected_at,
                       MAX(last_batch) AS last_batch
                FROM collection_progress
                GROUP BY platform, video_id
            ) ps ON ps.platform = v.platform AND ps.video_id = v.video_id
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
            """
        )
        with self.engine.connect() as connection:
            total = int(
                connection.execute(count_query, parameters).mappings().one()["total"]
            )
            page_parameters = {**parameters, "limit": limit, "offset": offset}
            items = _rows(
                connection.execute(data_query, page_parameters).mappings().all()
            )
        for item in items:
            for key in (
                "comment_count",
                "root_comment_count",
                "reply_comment_count",
                "creator_comment_count",
            ):
                item[key] = int(item[key] or 0)
        return _page_result(items, total, page, page_size)

    def get_video(
        self, video_id: str, *, platform: str = "douyin"
    ) -> dict[str, Any] | None:
        parameters = {"platform": platform, "video_id": video_id}
        with self.engine.connect() as connection:
            video = _row(
                connection.execute(
                    text(
                        """
                        SELECT platform, video_id, title, video_url,
                               first_seen_at, last_seen_at, created_at, updated_at
                        FROM videos
                        WHERE platform = :platform AND video_id = :video_id
                        """
                    ),
                    parameters,
                ).mappings().first()
            )
            if video is None:
                return None
            collections = _rows(
                connection.execute(
                    text(
                        """
                        SELECT cv.collection_id, c.name AS collection_name,
                               cv.episode, cv.card_metric,
                               cv.target_status AS status,
                               cv.manifest_generated_at
                        FROM collection_videos cv
                        JOIN collections c
                          ON c.platform = cv.platform
                         AND c.collection_id = cv.collection_id
                        WHERE cv.platform = :platform AND cv.video_id = :video_id
                        ORDER BY c.name, cv.episode
                        """
                    ),
                    parameters,
                ).mappings().all()
            )
            progress = _rows(
                connection.execute(
                    text(
                        """
                        SELECT scope_id, status, visible_comment_count,
                               stored_record_count, last_batch,
                               last_collected_at, notes, source_updated_at
                        FROM collection_progress
                        WHERE platform = :platform AND video_id = :video_id
                        ORDER BY source_updated_at DESC
                        """
                    ),
                    parameters,
                ).mappings().all()
            )
            stats = _row(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*) AS comment_count,
                               COALESCE(SUM(comment_type = 'root'), 0)
                                 AS root_comment_count,
                               COALESCE(SUM(comment_type = 'reply'), 0)
                                 AS reply_comment_count,
                               COALESCE(SUM(author_role = 'creator'), 0)
                                 AS creator_comment_count,
                               COALESCE(SUM(like_count), 0) AS total_like_count,
                               MAX(last_collected_at) AS last_collected_at,
                               (SELECT COUNT(*) FROM comment_snapshots snapshots
                                 WHERE snapshots.platform = :platform
                                   AND snapshots.video_id = :video_id)
                                 AS snapshot_count
                        FROM comments
                        WHERE platform = :platform AND video_id = :video_id
                        """
                    ),
                    parameters,
                ).mappings().one()
            )
            recent_jobs = _rows(
                connection.execute(
                    select(CollectionJob)
                    .where(CollectionJob.video_id == video_id)
                    .order_by(CollectionJob.created_at.desc(), CollectionJob.id.desc())
                    .limit(10)
                ).mappings().all()
            )
        assert stats is not None
        for key in (
            "comment_count",
            "root_comment_count",
            "reply_comment_count",
            "creator_comment_count",
            "total_like_count",
            "snapshot_count",
        ):
            stats[key] = int(stats[key] or 0)
        video["collections"] = collections
        video["progress"] = progress
        video["comment_stats"] = stats
        video["recent_jobs"] = recent_jobs
        if collections:
            video.update(
                {
                    "collection_name": collections[0]["collection_name"],
                    "episode": collections[0]["episode"],
                    "status": collections[0]["status"],
                }
            )
        if progress:
            for key in (
                "visible_comment_count",
                "stored_record_count",
                "last_batch",
                "last_collected_at",
                "notes",
            ):
                video[key] = progress[0][key]
        return video

    def search_comments(
        self,
        *,
        platform: str = "douyin",
        video_id: str | None = None,
        collection_id: str | None = None,
        query: str | None = None,
        comment_type: str | None = None,
        author_role: str | None = None,
        minimum_likes: int | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        limit, offset = _pagination(page, page_size)
        clauses = ["cm.platform = :platform"]
        parameters: dict[str, Any] = {"platform": platform}
        if video_id:
            clauses.append("cm.video_id = :video_id")
            parameters["video_id"] = video_id
        if collection_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM collection_videos cv "
                "WHERE cv.platform = cm.platform AND cv.video_id = cm.video_id "
                "AND cv.collection_id = :collection_id)"
            )
            parameters["collection_id"] = collection_id
        cleaned_query = _clean_optional_text(query)
        if cleaned_query:
            clauses.append("(cm.text LIKE :query OR cm.comment_id = :exact_query)")
            parameters["query"] = f"%{cleaned_query}%"
            parameters["exact_query"] = cleaned_query
        if comment_type:
            _require_choice(comment_type, _COMMENT_TYPES, field="comment type")
            clauses.append("cm.comment_type = :comment_type")
            parameters["comment_type"] = comment_type
        if author_role:
            _require_choice(author_role, _AUTHOR_ROLES, field="author role")
            clauses.append("cm.author_role = :author_role")
            parameters["author_role"] = author_role
        if minimum_likes is not None:
            if isinstance(minimum_likes, bool) or minimum_likes < 0:
                raise ValueError("minimum_likes must be non-negative")
            clauses.append("cm.like_count >= :minimum_likes")
            parameters["minimum_likes"] = minimum_likes

        order_by = {
            "newest": "COALESCE(cm.published_at, cm.last_collected_at) DESC, cm.comment_id",
            "published_desc": "COALESCE(cm.published_at, cm.last_collected_at) DESC, cm.comment_id",
            "oldest": "COALESCE(cm.published_at, cm.first_collected_at), cm.comment_id",
            "likes": "cm.like_count DESC, cm.last_collected_at DESC, cm.comment_id",
            "likes_desc": "cm.like_count DESC, cm.last_collected_at DESC, cm.comment_id",
            "replies": "cm.reply_count DESC, cm.last_collected_at DESC, cm.comment_id",
            "replies_desc": "cm.reply_count DESC, cm.last_collected_at DESC, cm.comment_id",
            "collected": "cm.last_collected_at DESC, cm.comment_id",
            "collected_desc": "cm.last_collected_at DESC, cm.comment_id",
        }.get(sort)
        if order_by is None:
            raise ValueError(f"invalid comment sort: {sort!r}")
        where_sql = " AND ".join(clauses)
        count_query = text(
            f"SELECT COUNT(*) AS total, MAX(cm.last_collected_at) AS last_collected_at "
            f"FROM comments cm WHERE {where_sql}"
        )
        data_query = text(
            f"""
            SELECT cm.platform, cm.comment_id, cm.comment_id_kind, cm.video_id,
                   v.title AS video_title, v.video_url, cm.parent_comment_id,
                   cm.root_comment_id, cm.comment_type, cm.author_role,
                   cm.text, cm.like_count, cm.reply_count, cm.published_at,
                   cm.published_label, cm.first_collected_at,
                   cm.last_collected_at, cm.collection_batch
            FROM comments cm
            JOIN videos v
              ON v.platform = cm.platform AND v.video_id = cm.video_id
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT :limit OFFSET :offset
            """
        )
        with self.engine.connect() as connection:
            summary = connection.execute(count_query, parameters).mappings().one()
            total = int(summary["total"])
            items = _rows(
                connection.execute(
                    data_query, {**parameters, "limit": limit, "offset": offset}
                ).mappings().all()
            )
        result = _page_result(items, total, page, page_size)
        result["last_collected_at"] = summary["last_collected_at"]
        return result

    def get_comment(
        self, comment_id: str, *, platform: str = "douyin"
    ) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            return _row(
                connection.execute(
                    text(
                        """
                        SELECT cm.*, v.title AS video_title, v.video_url
                        FROM comments cm
                        JOIN videos v
                          ON v.platform = cm.platform AND v.video_id = cm.video_id
                        WHERE cm.platform = :platform
                          AND cm.comment_id = :comment_id
                        """
                    ),
                    {"platform": platform, "comment_id": comment_id},
                ).mappings().first()
            )

    def get_comment_thread(
        self, comment_id: str, *, platform: str = "douyin"
    ) -> dict[str, Any] | None:
        parameters = {"platform": platform, "comment_id": comment_id}
        with self.engine.connect() as connection:
            selected = _row(
                connection.execute(
                    text(
                        """
                        SELECT * FROM comments
                        WHERE platform = :platform AND comment_id = :comment_id
                        """
                    ),
                    parameters,
                ).mappings().first()
            )
            if selected is None:
                return None
            root_id = (
                selected["comment_id"]
                if selected["comment_type"] == "root"
                else selected["root_comment_id"]
            )
            thread_rows = _rows(
                connection.execute(
                    text(
                        """
                        SELECT cm.*, v.title AS video_title, v.video_url,
                               parent.text AS reply_to_text,
                               CASE
                                 WHEN cm.comment_id = :root_id THEN 0
                                 WHEN cm.parent_comment_id = :root_id THEN 1
                                 ELSE 2
                               END AS depth,
                               (cm.parent_comment_id = :root_id) AS parent_is_root
                        FROM comments cm
                        JOIN videos v
                          ON v.platform = cm.platform AND v.video_id = cm.video_id
                        LEFT JOIN comments parent
                          ON parent.platform = cm.platform
                         AND parent.comment_id = cm.parent_comment_id
                        WHERE cm.platform = :platform
                          AND (cm.comment_id = :root_id
                               OR cm.root_comment_id = :root_id)
                        ORDER BY (cm.comment_id = :root_id) DESC,
                                 COALESCE(cm.published_at, cm.first_collected_at),
                                 cm.comment_id
                        """
                    ),
                    {"platform": platform, "root_id": root_id},
                ).mappings().all()
            )
        root = next((item for item in thread_rows if item["comment_id"] == root_id), None)
        if root is None:
            return None
        return {
            "selected_comment_id": comment_id,
            "root": root,
            "replies": [item for item in thread_rows if item["comment_id"] != root_id],
        }

    def list_comment_snapshots(
        self,
        comment_id: str,
        *,
        platform: str = "douyin",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        limit, offset = _pagination(page, page_size)
        parameters = {"platform": platform, "comment_id": comment_id}
        with self.engine.connect() as connection:
            total = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*) AS total FROM comment_snapshots
                        WHERE platform = :platform AND comment_id = :comment_id
                        """
                    ),
                    parameters,
                ).mappings().one()["total"]
            )
            items = _rows(
                connection.execute(
                    text(
                        """
                        SELECT snapshot_id, platform, comment_id, collected_at,
                               text, like_count, reply_count, published_at,
                               published_label, collection_batch, imported_at
                        FROM comment_snapshots
                        WHERE platform = :platform AND comment_id = :comment_id
                        ORDER BY collected_at DESC, snapshot_id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    {**parameters, "limit": limit, "offset": offset},
                ).mappings().all()
            )
        return _page_result(items, total, page, page_size)

    def list_import_runs(
        self, *, page: int = 1, page_size: int = 25
    ) -> dict[str, Any]:
        limit, offset = _pagination(page, page_size, maximum=100)
        with self.engine.connect() as connection:
            total = int(
                connection.execute(text("SELECT COUNT(*) AS total FROM import_runs"))
                .mappings()
                .one()["total"]
            )
            items = _rows(
                connection.execute(
                    text(
                        """
                        SELECT import_run_id, source_path, source_sha256,
                               started_at, completed_at, status,
                               raw_comment_count, duplicate_source_count,
                               stored_comment_count, inserted_snapshot_count,
                               collection_count, target_video_count,
                               progress_video_count
                        FROM import_runs
                        ORDER BY completed_at DESC, import_run_id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    {"limit": limit, "offset": offset},
                ).mappings().all()
            )
        return _page_result(items, total, page, page_size)

    def import_stats(self, *, platform: str = "douyin") -> dict[str, Any]:
        """Return the aggregate values used by the import management page."""

        with self.engine.connect() as connection:
            stats = _row(
                connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM comments
                            WHERE platform = :platform) AS comment_count,
                          (SELECT COUNT(*) FROM comment_snapshots
                            WHERE platform = :platform) AS snapshot_count,
                          (SELECT COUNT(*) FROM import_runs) AS import_count,
                          (SELECT COUNT(*) FROM import_runs
                            WHERE status = 'success') AS success_count,
                          latest.completed_at AS last_import_at,
                          latest.source_path AS last_source_path,
                          COALESCE(latest.inserted_snapshot_count, 0)
                            AS snapshot_growth
                        FROM (SELECT 1) seed
                        LEFT JOIN (
                            SELECT completed_at, source_path,
                                   inserted_snapshot_count
                            FROM import_runs
                            ORDER BY completed_at DESC, import_run_id DESC
                            LIMIT 1
                        ) latest ON 1 = 1
                        """
                    ),
                    {"platform": platform},
                ).mappings().one()
            )
        assert stats is not None
        for key in (
            "comment_count",
            "snapshot_count",
            "import_count",
            "success_count",
            "snapshot_growth",
        ):
            stats[key] = int(stats[key] or 0)
        return stats

    def create_job(
        self,
        job_type: str,
        *,
        video_id: str | None = None,
        scheduled_for: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        _require_choice(job_type, JOB_TYPES, field="job type")
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        now = utc_now()
        values = {
            "job_type": job_type,
            "status": "queued",
            "video_id": _clean_optional_text(video_id),
            "scheduled_for": normalize_utc(scheduled_for) or now,
            "payload": dict(payload or {}),
            "attempts": 0,
            "max_attempts": max_attempts,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            result = connection.execute(insert(CollectionJob).values(**values))
            job_id = int(result.inserted_primary_key[0])
            job = self._get_job(connection, job_id)
        assert job is not None
        return job

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            return self._get_job(connection, job_id)

    def _get_job(self, connection: Connection, job_id: int) -> dict[str, Any] | None:
        return _row(
            connection.execute(
                select(CollectionJob).where(CollectionJob.id == job_id)
            ).mappings().first()
        )

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        video_id: str | None = None,
        sort: str = "newest",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        limit, offset = _pagination(page, page_size, maximum=100)
        conditions = []
        if status:
            _require_choice(status, JOB_STATUSES, field="job status")
            conditions.append(CollectionJob.status == status)
        if job_type:
            _require_choice(job_type, JOB_TYPES, field="job type")
            conditions.append(CollectionJob.job_type == job_type)
        if video_id:
            conditions.append(CollectionJob.video_id == video_id)
        count_statement = select(text("COUNT(*) AS total")).select_from(CollectionJob)
        data_statement = select(CollectionJob)
        if conditions:
            predicate = and_(*conditions)
            count_statement = count_statement.where(predicate)
            data_statement = data_statement.where(predicate)
        order_by = {
            "newest": (CollectionJob.created_at.desc(), CollectionJob.id.desc()),
            "scheduled": (CollectionJob.scheduled_for, CollectionJob.id),
            "status": (
                case(
                    (CollectionJob.status == "running", 0),
                    (CollectionJob.status == "queued", 1),
                    (CollectionJob.status == "blocked", 2),
                    (CollectionJob.status == "failed", 3),
                    (CollectionJob.status == "succeeded", 4),
                    else_=5,
                ),
                CollectionJob.created_at.desc(),
                CollectionJob.id.desc(),
            ),
        }.get(sort)
        if order_by is None:
            raise ValueError(f"invalid job sort: {sort!r}")
        data_statement = data_statement.order_by(*order_by).limit(limit).offset(offset)
        with self.engine.connect() as connection:
            total = int(connection.execute(count_statement).mappings().one()["total"])
            items = _rows(connection.execute(data_statement).mappings().all())
        return _page_result(items, total, page, page_size)

    def claim_next_job(
        self, worker_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id cannot be empty")
        claimed_at = normalize_utc(now) or utc_now()
        with self.engine.begin() as connection:
            job_id = connection.execute(
                select(CollectionJob.id)
                .where(
                    CollectionJob.status == "queued",
                    CollectionJob.scheduled_for <= claimed_at,
                    CollectionJob.attempts < CollectionJob.max_attempts,
                )
                .order_by(CollectionJob.scheduled_for, CollectionJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if job_id is None:
                return None
            connection.execute(
                update(CollectionJob)
                .where(CollectionJob.id == job_id)
                .values(
                    status="running",
                    worker_id=worker_id,
                    attempts=CollectionJob.attempts + 1,
                    claimed_at=claimed_at,
                    heartbeat_at=claimed_at,
                    started_at=claimed_at,
                    finished_at=None,
                    message=None,
                    error=None,
                    result=None,
                    updated_at=claimed_at,
                )
            )
            return self._get_job(connection, int(job_id))

    def heartbeat_job(self, job_id: int, worker_id: str) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionJob)
                .where(
                    CollectionJob.id == job_id,
                    CollectionJob.status == "running",
                    CollectionJob.worker_id == worker_id,
                )
                .values(heartbeat_at=now, updated_at=now)
            )
        return result.rowcount == 1

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        message: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        result: Mapping[str, Any] | None | object = _UNSET,
        scheduled_for: datetime | None | object = _UNSET,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.engine.begin() as connection:
            current = self._get_job_for_update(connection, job_id)
            if current is None:
                return None
            values: dict[str, Any] = {"updated_at": now}
            if status is not None and status != current["status"]:
                _require_choice(status, JOB_STATUSES, field="job status")
                allowed = _JOB_TRANSITIONS[current["status"]]
                if status not in allowed:
                    raise ValueError(
                        f"invalid job transition: {current['status']} -> {status}"
                    )
                values["status"] = status
                if status in {"succeeded", "failed", "blocked", "cancelled"}:
                    values["finished_at"] = now
                elif status == "queued":
                    values.update(
                        worker_id=None,
                        claimed_at=None,
                        heartbeat_at=None,
                        started_at=None,
                        finished_at=None,
                    )
            if message is not _UNSET:
                values["message"] = message
            if error is not _UNSET:
                values["error"] = error
            if result is not _UNSET:
                values["result"] = None if result is None else dict(result)
            if scheduled_for is not _UNSET:
                values["scheduled_for"] = normalize_utc(scheduled_for) or now
            connection.execute(
                update(CollectionJob).where(CollectionJob.id == job_id).values(**values)
            )
            return self._get_job(connection, job_id)

    def _get_job_for_update(
        self, connection: Connection, job_id: int
    ) -> dict[str, Any] | None:
        return _row(
            connection.execute(
                select(CollectionJob)
                .where(CollectionJob.id == job_id)
                .with_for_update()
            ).mappings().first()
        )

    def cancel_job(self, job_id: int, *, message: str | None = None) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionJob)
                .where(
                    CollectionJob.id == job_id,
                    CollectionJob.status.in_(("queued", "blocked", "failed")),
                )
                .values(
                    status="cancelled",
                    message=message,
                    finished_at=now,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    def requeue_job(
        self, job_id: int, *, scheduled_for: datetime | None = None
    ) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionJob)
                .where(
                    CollectionJob.id == job_id,
                    CollectionJob.status.in_(("failed", "blocked")),
                    CollectionJob.attempts < CollectionJob.max_attempts,
                )
                .values(
                    status="queued",
                    scheduled_for=normalize_utc(scheduled_for) or now,
                    worker_id=None,
                    claimed_at=None,
                    heartbeat_at=None,
                    started_at=None,
                    finished_at=None,
                    message=None,
                    error=None,
                    result=None,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    def mark_job_succeeded(
        self,
        job_id: int,
        result: Mapping[str, Any] | None = None,
        message: str | None = None,
        worker_id: str | None = None,
    ) -> bool:
        return self._finish_running_job(
            job_id,
            "succeeded",
            worker_id=worker_id,
            result=result,
            message=message,
            error=None,
        )

    def mark_job_blocked(
        self,
        job_id: int,
        message: str,
        result: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> bool:
        return self._finish_running_job(
            job_id,
            "blocked",
            worker_id=worker_id,
            result=result,
            message=message,
            error=None,
        )

    def mark_job_failed(
        self, job_id: int, message: str, worker_id: str | None = None
    ) -> bool:
        return self._finish_running_job(
            job_id,
            "failed",
            worker_id=worker_id,
            result=None,
            message="Task failed",
            error=message,
        )

    def _finish_running_job(
        self,
        job_id: int,
        status: str,
        *,
        worker_id: str | None,
        result: Mapping[str, Any] | None,
        message: str | None,
        error: str | None,
    ) -> bool:
        now = utc_now()
        conditions = [CollectionJob.id == job_id, CollectionJob.status == "running"]
        if worker_id is not None:
            conditions.append(CollectionJob.worker_id == worker_id)
        with self.engine.begin() as connection:
            update_result = connection.execute(
                update(CollectionJob)
                .where(*conditions)
                .values(
                    status=status,
                    result=None if result is None else dict(result),
                    message=message,
                    error=error,
                    heartbeat_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
        return update_result.rowcount == 1

    def requeue_stale_jobs(
        self, *, stale_after: timedelta | None = timedelta(minutes=10)
    ) -> int:
        """Recover abandoned running jobs, or all running jobs when ``None``.

        Passing ``None`` is safe only after the caller holds the process-wide
        worker lock, which proves no live worker still owns those claims.
        """
        if stale_after is not None and stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        now = utc_now()
        stale_conditions = [CollectionJob.status == "running"]
        if stale_after is not None:
            cutoff = now - stale_after
            stale_conditions.append(
                or_(
                    CollectionJob.heartbeat_at.is_(None),
                    CollectionJob.heartbeat_at < cutoff,
                )
            )
        with self.engine.begin() as connection:
            failed = connection.execute(
                update(CollectionJob)
                .where(
                    *stale_conditions,
                    CollectionJob.attempts >= CollectionJob.max_attempts,
                )
                .values(
                    status="failed",
                    message="Task failed",
                    error="Worker stopped during the final attempt",
                    heartbeat_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            requeued = connection.execute(
                update(CollectionJob)
                .where(
                    *stale_conditions,
                    CollectionJob.attempts < CollectionJob.max_attempts,
                )
                .values(
                    status="queued",
                    scheduled_for=now,
                    worker_id=None,
                    claimed_at=None,
                    heartbeat_at=None,
                    started_at=None,
                    finished_at=None,
                    message="Recovered after worker heartbeat timeout",
                    error=None,
                    updated_at=now,
                )
            )
        return int(failed.rowcount + requeued.rowcount)

    def create_schedule(
        self,
        name: str,
        job_type: str,
        schedule_type: str,
        schedule_config: Mapping[str, Any],
        *,
        video_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        enabled: bool = True,
        next_run_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_choice(job_type, JOB_TYPES, field="job type")
        _require_choice(schedule_type, SCHEDULE_TYPES, field="schedule type")
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("schedule name cannot be empty")
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                insert(CollectionSchedule).values(
                    name=cleaned_name,
                    job_type=job_type,
                    video_id=_clean_optional_text(video_id),
                    schedule_type=schedule_type,
                    schedule_config=dict(schedule_config),
                    payload=dict(payload or {}),
                    enabled=bool(enabled),
                    next_run_at=normalize_utc(next_run_at),
                    created_at=now,
                    updated_at=now,
                )
            )
            schedule_id = int(result.inserted_primary_key[0])
            schedule = self._get_schedule(connection, schedule_id)
        assert schedule is not None
        return schedule

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            return self._get_schedule(connection, schedule_id)

    def _get_schedule(
        self, connection: Connection, schedule_id: int
    ) -> dict[str, Any] | None:
        return _row(
            connection.execute(
                select(CollectionSchedule).where(CollectionSchedule.id == schedule_id)
            ).mappings().first()
        )

    def list_schedules(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        statement = select(CollectionSchedule)
        if enabled is not None:
            statement = statement.where(CollectionSchedule.enabled == enabled)
        statement = statement.order_by(
            CollectionSchedule.enabled.desc(),
            CollectionSchedule.next_run_at,
            CollectionSchedule.id,
        )
        with self.engine.connect() as connection:
            return _rows(connection.execute(statement).mappings().all())

    def update_schedule(
        self,
        schedule_id: int,
        *,
        name: str | object = _UNSET,
        schedule_type: str | object = _UNSET,
        schedule_config: Mapping[str, Any] | object = _UNSET,
        payload: Mapping[str, Any] | object = _UNSET,
        enabled: bool | object = _UNSET,
        next_run_at: datetime | None | object = _UNSET,
    ) -> dict[str, Any] | None:
        values: dict[str, Any] = {"updated_at": utc_now()}
        if name is not _UNSET:
            cleaned_name = str(name).strip()
            if not cleaned_name:
                raise ValueError("schedule name cannot be empty")
            values["name"] = cleaned_name
        if schedule_type is not _UNSET:
            values["schedule_type"] = _require_choice(
                str(schedule_type), SCHEDULE_TYPES, field="schedule type"
            )
        if schedule_config is not _UNSET:
            values["schedule_config"] = dict(schedule_config)
        if payload is not _UNSET:
            values["payload"] = dict(payload)
        if enabled is not _UNSET:
            values["enabled"] = bool(enabled)
        if next_run_at is not _UNSET:
            values["next_run_at"] = normalize_utc(next_run_at)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionSchedule)
                .where(CollectionSchedule.id == schedule_id)
                .values(**values)
            )
            if result.rowcount != 1:
                return None
            return self._get_schedule(connection, schedule_id)

    def set_schedule_enabled(
        self,
        schedule_id: int,
        enabled: bool,
        *,
        next_run_at: datetime | None | object = _UNSET,
    ) -> bool:
        values: dict[str, Any] = {"enabled": bool(enabled), "updated_at": utc_now()}
        if next_run_at is not _UNSET:
            values["next_run_at"] = normalize_utc(next_run_at)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionSchedule)
                .where(CollectionSchedule.id == schedule_id)
                .values(**values)
            )
        return result.rowcount == 1

    def list_due_schedules(
        self, *, now: datetime | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        due_at = normalize_utc(now) or utc_now()
        with self.engine.connect() as connection:
            return _rows(
                connection.execute(
                    select(CollectionSchedule)
                    .where(
                        CollectionSchedule.enabled.is_(True),
                        CollectionSchedule.next_run_at.is_not(None),
                        CollectionSchedule.next_run_at <= due_at,
                    )
                    .order_by(CollectionSchedule.next_run_at, CollectionSchedule.id)
                    .limit(limit)
                ).mappings().all()
            )

    def mark_schedule_enqueued(
        self,
        schedule_id: int,
        *,
        next_run_at: datetime | None,
        enqueued_at: datetime | None = None,
    ) -> bool:
        now = normalize_utc(enqueued_at) or utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(CollectionSchedule)
                .where(CollectionSchedule.id == schedule_id)
                .values(
                    last_enqueued_at=now,
                    next_run_at=normalize_utc(next_run_at),
                    updated_at=now,
                )
            )
        return result.rowcount == 1
