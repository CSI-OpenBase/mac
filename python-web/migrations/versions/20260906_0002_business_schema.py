"""Create the versioned creator data schema.

Revision ID: 20260906_0002
Revises: 20260906_0001
Create Date: 2026-09-06
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

from alembic import op


revision: str = "20260906_0002"
down_revision: str | None = "20260906_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "resources" / "business-schema-v1.sql"
SCHEMA_SHA256 = "cab922d80a142a45a31032c90a4912b48886d742bf440d231c91c8ddbcb7845e"


def _schema_statements() -> list[str]:
    content = SCHEMA_PATH.read_bytes()
    if hashlib.sha256(content).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError(
            "business schema v1 changed after its migration was published"
        )
    statements = [statement.strip() for statement in content.decode("utf-8-sig").split(";")]
    return [statement for statement in statements if statement]


def upgrade() -> None:
    connection = op.get_bind()
    for statement in _schema_statements():
        connection.exec_driver_sql(statement)


def downgrade() -> None:
    for table in (
        "audience_snapshots",
        "creator_profile_snapshots",
        "work_metric_snapshots",
        "creator_works",
        "import_runs",
        "comment_tags",
        "comment_topics",
        "comment_snapshots",
        "comments",
        "collection_progress",
        "collection_videos",
        "videos",
        "collections",
        "workspace_identity",
    ):
        op.execute(f"DROP TABLE IF EXISTS `{table}`")
