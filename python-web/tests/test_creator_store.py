from __future__ import annotations

import json
from datetime import datetime

import pytest

from admin_app.creator_store import CreatorStoreError, _validate_snapshot_conflicts


class SnapshotCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.parameters = []

    def execute(self, query, parameters):
        self.query = query
        self.parameters = parameters

    def fetchall(self):
        return self.rows


def test_creator_snapshot_identity_rejects_different_record_json() -> None:
    record = {
        "platform": "douyin",
        "observed_at": "2026-09-06T03:00:00Z",
        "display_name": "Creator",
        "follower_count": 10,
        "following_count": 2,
        "total_like_count": 100,
        "work_count": 3,
    }
    cursor = SnapshotCursor(
        [
            {
                "platform": "douyin",
                "observed_at": datetime(2026, 9, 6, 3),
                "record_json": json.dumps(
                    {**record, "follower_count": 11}, ensure_ascii=False
                ),
            }
        ]
    )

    with pytest.raises(CreatorStoreError, match="conflicting MySQL snapshot"):
        _validate_snapshot_conflicts(
            cursor,
            table="creator_profile_snapshots",
            identity_columns=("platform", "observed_at"),
            records=[record],
        )


def test_creator_snapshot_identity_accepts_same_record_json() -> None:
    record = {
        "platform": "douyin",
        "observed_at": "2026-09-06T03:00:00Z",
        "dimension": "age",
        "segment": "25-34",
        "share": 0.5,
        "sample_size": None,
    }
    cursor = SnapshotCursor(
        [
            {
                "platform": "douyin",
                "observed_at": datetime(2026, 9, 6, 3),
                "dimension_name": "age",
                "segment_name": "25-34",
                "record_json": record,
            }
        ]
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
        records=[record],
    )

    assert "FROM `audience_snapshots`" in cursor.query
    assert cursor.parameters == ["douyin"]
