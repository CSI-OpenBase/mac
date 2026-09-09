"""Version-one MySQL schema contract shared by startup and import verification."""

from __future__ import annotations

from collections.abc import Mapping, Set


class SchemaContractError(RuntimeError):
    """Raised when an existing database cannot satisfy the current application."""


EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "workspace_identity": frozenset(
        {"singleton_id", "workspace_slug", "platform", "created_at", "updated_at"}
    ),
    "collections": frozenset(
        {
            "platform", "collection_id", "name", "declared_episode_count",
            "source_scope_id", "manifest_generated_at", "created_at", "updated_at",
        }
    ),
    "videos": frozenset(
        {
            "platform", "video_id", "title", "video_url", "first_seen_at",
            "last_seen_at", "created_at", "updated_at",
        }
    ),
    "collection_videos": frozenset(
        {
            "platform", "collection_id", "video_id", "episode", "card_metric",
            "target_status", "manifest_generated_at", "created_at", "updated_at",
        }
    ),
    "collection_progress": frozenset(
        {
            "platform", "scope_id", "video_id", "status", "visible_comment_count",
            "stored_record_count", "last_batch", "last_collected_at", "notes",
            "source_updated_at", "created_at", "updated_at",
        }
    ),
    "comments": frozenset(
        {
            "platform", "comment_id", "schema_version", "comment_id_kind", "video_id",
            "parent_comment_id", "root_comment_id", "comment_type", "author_role", "text",
            "like_count", "reply_count", "published_at", "published_label",
            "first_collected_at", "last_collected_at", "source_url",
            "collection_batch", "created_at", "updated_at",
        }
    ),
    "comment_snapshots": frozenset(
        {
            "snapshot_id", "platform", "comment_id", "collected_at", "schema_version",
            "comment_id_kind", "video_id", "video_title", "video_url",
            "parent_comment_id", "root_comment_id", "comment_type", "author_role", "text",
            "like_count", "reply_count", "published_at", "published_label", "source_url",
            "collection_batch", "record_json", "imported_at",
        }
    ),
    "comment_topics": frozenset(
        {"platform", "comment_id", "topic_id", "created_at"}
    ),
    "comment_tags": frozenset(
        {"platform", "comment_id", "tag", "created_at"}
    ),
    "import_runs": frozenset(
        {
            "import_run_id", "source_path", "source_sha256", "target_manifest_path",
            "target_manifest_sha256", "progress_path", "progress_sha256", "started_at",
            "completed_at", "status", "raw_comment_count", "duplicate_source_count",
            "stored_comment_count", "inserted_snapshot_count", "collection_count",
            "target_video_count", "progress_video_count", "created_at",
        }
    ),
    "creator_works": frozenset(
        {
            "platform", "work_id", "work_id_kind", "title", "tags", "published_at",
            "content_type", "audit_status", "first_observed_at", "last_observed_at",
            "created_at", "updated_at",
        }
    ),
    "work_metric_snapshots": frozenset(
        {
            "snapshot_id", "platform", "work_id", "observed_at", "view_count",
            "like_count", "share_count", "comment_count", "collect_count",
            "profile_visit_count", "follower_gain", "completion_rate",
            "five_second_completion_rate", "cover_click_rate", "two_second_bounce_rate",
            "average_watch_seconds", "source_file", "source_sheet", "record_json",
            "imported_at",
        }
    ),
    "creator_profile_snapshots": frozenset(
        {
            "snapshot_id", "platform", "observed_at", "display_name", "follower_count",
            "following_count", "total_like_count", "work_count", "record_json",
            "imported_at",
        }
    ),
    "audience_snapshots": frozenset(
        {
            "snapshot_id", "platform", "observed_at", "dimension_name", "segment_name",
            "share_value", "sample_size", "record_json", "imported_at",
        }
    ),
}

EXPECTED_UNIQUE_KEYS: dict[str, frozenset[tuple[str, ...]]] = {
    "workspace_identity": frozenset({("singleton_id",)}),
    "collections": frozenset({("platform", "collection_id")}),
    "videos": frozenset({("platform", "video_id")}),
    "collection_videos": frozenset(
        {
            ("platform", "collection_id", "video_id"),
            ("platform", "collection_id", "episode"),
        }
    ),
    "collection_progress": frozenset({("platform", "scope_id", "video_id")}),
    "comments": frozenset({("platform", "comment_id")}),
    "comment_snapshots": frozenset({("platform", "comment_id", "collected_at")}),
    "comment_topics": frozenset({("platform", "comment_id", "topic_id")}),
    "comment_tags": frozenset({("platform", "comment_id", "tag")}),
    "import_runs": frozenset({("import_run_id",)}),
    "creator_works": frozenset({("platform", "work_id")}),
    "work_metric_snapshots": frozenset({("platform", "work_id", "observed_at")}),
    "creator_profile_snapshots": frozenset({("platform", "observed_at")}),
    "audience_snapshots": frozenset(
        {("platform", "observed_at", "dimension_name", "segment_name")}
    ),
}

REQUIRED_TABLES = tuple(EXPECTED_COLUMNS)

ADMIN_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "collection_jobs": frozenset(
        {
            "id", "job_type", "status", "video_id", "scheduled_for", "payload",
            "result", "message", "error", "attempts", "max_attempts", "worker_id",
            "claimed_at", "heartbeat_at", "started_at", "finished_at", "created_at",
            "updated_at",
        }
    ),
    "collection_schedules": frozenset(
        {
            "id", "name", "job_type", "video_id", "schedule_type", "schedule_config",
            "payload", "enabled", "next_run_at", "last_enqueued_at", "created_at",
            "updated_at",
        }
    ),
}
ADMIN_EXPECTED_UNIQUE_KEYS: dict[str, frozenset[tuple[str, ...]]] = {
    "collection_jobs": frozenset({("id",)}),
    "collection_schedules": frozenset({("id",)}),
}
STARTUP_EXPECTED_COLUMNS = {**EXPECTED_COLUMNS, **ADMIN_EXPECTED_COLUMNS}
STARTUP_EXPECTED_UNIQUE_KEYS = {
    **EXPECTED_UNIQUE_KEYS,
    **ADMIN_EXPECTED_UNIQUE_KEYS,
}
STARTUP_REQUIRED_TABLES = tuple(STARTUP_EXPECTED_COLUMNS)


def validate_schema_contract(
    actual_columns: Mapping[str, Set[str]],
    actual_unique_keys: Mapping[str, Set[tuple[str, ...]]],
) -> None:
    _validate_contract(
        actual_columns,
        actual_unique_keys,
        expected_columns=EXPECTED_COLUMNS,
        expected_unique_keys=EXPECTED_UNIQUE_KEYS,
    )


def validate_startup_schema_contract(
    actual_columns: Mapping[str, Set[str]],
    actual_unique_keys: Mapping[str, Set[tuple[str, ...]]],
) -> None:
    _validate_contract(
        actual_columns,
        actual_unique_keys,
        expected_columns=STARTUP_EXPECTED_COLUMNS,
        expected_unique_keys=STARTUP_EXPECTED_UNIQUE_KEYS,
    )


def _validate_contract(
    actual_columns: Mapping[str, Set[str]],
    actual_unique_keys: Mapping[str, Set[tuple[str, ...]]],
    *,
    expected_columns: Mapping[str, Set[str]],
    expected_unique_keys: Mapping[str, Set[tuple[str, ...]]],
) -> None:
    missing_tables = sorted(set(expected_columns) - set(actual_columns))
    if missing_tables:
        raise SchemaContractError(
            "missing required MySQL tables: " + ", ".join(missing_tables)
        )
    problems: list[str] = []
    for table, expected in expected_columns.items():
        missing = sorted(expected - set(actual_columns.get(table, set())))
        if missing:
            problems.append(f"{table} missing columns: {', '.join(missing)}")
    for table, expected in expected_unique_keys.items():
        missing = expected - set(actual_unique_keys.get(table, set()))
        if missing:
            rendered = ", ".join("(" + ", ".join(key) + ")" for key in sorted(missing))
            problems.append(f"{table} missing unique keys: {rendered}")
    if problems:
        raise SchemaContractError("; ".join(problems))
