"""SQLAlchemy models owned by the local administration application.

The pre-existing archive tables intentionally are not mapped here.  They have
composite primary keys and remain managed by ``mysql-schema.sql`` and the import
pipeline; ``Repository`` accesses them with bound SQL text queries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .database import utc_now


JOB_TYPES = frozenset({"collect_video", "sync_mysql", "analyze"})
JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "blocked", "cancelled"}
)
SCHEDULE_TYPES = frozenset({"cron", "interval", "once"})


class AdminBase(DeclarativeBase):
    """Metadata containing only tables owned by the administration app."""


class CollectionJob(AdminBase):
    __tablename__ = "collection_jobs"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('collect_video', 'sync_mysql', 'analyze')",
            name="chk_collection_jobs_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'blocked', 'cancelled')",
            name="chk_collection_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="chk_collection_jobs_attempts"),
        CheckConstraint("max_attempts > 0", name="chk_collection_jobs_max_attempts"),
        Index("ix_collection_jobs_queue", "status", "scheduled_for", "id"),
        Index("ix_collection_jobs_video", "video_id", "created_at"),
        Index("ix_collection_jobs_worker", "worker_id", "status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"},
    )

    id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'queued'")
    )
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(mysql.JSON, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(mysql.JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(mysql.LONGTEXT, nullable=True)
    attempts: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("3")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=6), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=6), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=6), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=False, default=utc_now,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class CollectionSchedule(AdminBase):
    __tablename__ = "collection_schedules"
    __table_args__ = (
        CheckConstraint(
            "job_type IN ('collect_video', 'sync_mysql', 'analyze')",
            name="chk_collection_schedules_job_type",
        ),
        CheckConstraint(
            "schedule_type IN ('cron', 'interval', 'once')",
            name="chk_collection_schedules_type",
        ),
        Index("ix_collection_schedules_due", "enabled", "next_run_at", "id"),
        Index("ix_collection_schedules_video", "video_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"},
    )

    id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_config: Mapped[dict[str, Any]] = mapped_column(mysql.JSON, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(mysql.JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    next_run_at: Mapped[datetime | None] = mapped_column(mysql.DATETIME(fsp=6), nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=False, default=utc_now,
        server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )
