"""Create collection job and schedule tables.

Revision ID: 20260906_0001
Revises: None
Create Date: 2026-09-06
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "20260906_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_jobs",
        sa.Column(
            "id", mysql.BIGINT(unsigned=True), nullable=False, autoincrement=True
        ),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("video_id", sa.String(length=64), nullable=True),
        sa.Column("scheduled_for", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("payload", mysql.JSON(), nullable=False),
        sa.Column("result", mysql.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", mysql.LONGTEXT(), nullable=True),
        sa.Column(
            "attempts", mysql.INTEGER(unsigned=True), nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "max_attempts", mysql.INTEGER(unsigned=True), nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("heartbeat_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at", mysql.DATETIME(fsp=6), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at", mysql.DATETIME(fsp=6), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint(
            "job_type IN ('collect_video', 'sync_mysql', 'analyze')",
            name="chk_collection_jobs_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'blocked', 'cancelled')",
            name="chk_collection_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="chk_collection_jobs_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="chk_collection_jobs_max_attempts"),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_collection_jobs_queue",
        "collection_jobs",
        ["status", "scheduled_for", "id"],
    )
    op.create_index(
        "ix_collection_jobs_video", "collection_jobs", ["video_id", "created_at"]
    )
    op.create_index(
        "ix_collection_jobs_worker", "collection_jobs", ["worker_id", "status"]
    )

    op.create_table(
        "collection_schedules",
        sa.Column(
            "id", mysql.BIGINT(unsigned=True), nullable=False, autoincrement=True
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("video_id", sa.String(length=64), nullable=True),
        sa.Column("schedule_type", sa.String(length=16), nullable=False),
        sa.Column("schedule_config", mysql.JSON(), nullable=False),
        sa.Column("payload", mysql.JSON(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("next_run_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_enqueued_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at", mysql.DATETIME(fsp=6), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at", mysql.DATETIME(fsp=6), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint(
            "job_type IN ('collect_video', 'sync_mysql', 'analyze')",
            name="chk_collection_schedules_job_type",
        ),
        sa.CheckConstraint(
            "schedule_type IN ('cron', 'interval', 'once')",
            name="chk_collection_schedules_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_collection_schedules_due",
        "collection_schedules",
        ["enabled", "next_run_at", "id"],
    )
    op.create_index(
        "ix_collection_schedules_video", "collection_schedules", ["video_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_collection_schedules_video", table_name="collection_schedules")
    op.drop_index("ix_collection_schedules_due", table_name="collection_schedules")
    op.drop_table("collection_schedules")
    op.drop_index("ix_collection_jobs_worker", table_name="collection_jobs")
    op.drop_index("ix_collection_jobs_video", table_name="collection_jobs")
    op.drop_index("ix_collection_jobs_queue", table_name="collection_jobs")
    op.drop_table("collection_jobs")
