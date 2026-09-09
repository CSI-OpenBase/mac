-- Queryable long-term store for one CSI OpenBase creator workspace.
-- The importer selects the target database before executing this file.

CREATE TABLE IF NOT EXISTS workspace_identity (
    singleton_id TINYINT UNSIGNED NOT NULL,
    workspace_slug VARCHAR(48) NOT NULL,
    platform VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (singleton_id),
    UNIQUE KEY uq_workspace_identity_slug (workspace_slug),
    CONSTRAINT chk_workspace_identity_singleton CHECK (singleton_id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collections (
    platform VARCHAR(32) NOT NULL,
    collection_id VARCHAR(64) NOT NULL,
    name VARCHAR(512) NOT NULL,
    declared_episode_count INT UNSIGNED NOT NULL,
    source_scope_id VARCHAR(128) NOT NULL,
    manifest_generated_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, collection_id),
    CONSTRAINT chk_collections_episode_count
        CHECK (declared_episode_count >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS videos (
    platform VARCHAR(32) NOT NULL,
    video_id VARCHAR(64) NOT NULL,
    title LONGTEXT NOT NULL,
    video_url VARCHAR(2048) NOT NULL,
    first_seen_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, video_id),
    CONSTRAINT chk_videos_seen_order
        CHECK (last_seen_at >= first_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collection_videos (
    platform VARCHAR(32) NOT NULL,
    collection_id VARCHAR(64) NOT NULL,
    video_id VARCHAR(64) NOT NULL,
    episode INT UNSIGNED NULL,
    card_metric VARCHAR(64) NOT NULL DEFAULT '',
    target_status VARCHAR(16) NOT NULL,
    manifest_generated_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, collection_id, video_id),
    UNIQUE KEY uq_collection_videos_episode (platform, collection_id, episode),
    KEY ix_collection_videos_video (platform, video_id),
    CONSTRAINT fk_collection_videos_collection
        FOREIGN KEY (platform, collection_id)
        REFERENCES collections (platform, collection_id),
    CONSTRAINT fk_collection_videos_video
        FOREIGN KEY (platform, video_id)
        REFERENCES videos (platform, video_id),
    CONSTRAINT chk_collection_videos_status
        CHECK (target_status IN ('pending', 'partial', 'complete', 'blocked')),
    CONSTRAINT chk_collection_videos_episode
        CHECK (episode IS NULL OR episode > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS collection_progress (
    platform VARCHAR(32) NOT NULL,
    scope_id VARCHAR(128) NOT NULL,
    video_id VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    visible_comment_count BIGINT UNSIGNED NOT NULL,
    stored_record_count BIGINT UNSIGNED NOT NULL,
    last_batch VARCHAR(255) NOT NULL DEFAULT '',
    last_collected_at DATETIME(6) NULL,
    notes LONGTEXT NOT NULL,
    source_updated_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, scope_id, video_id),
    KEY ix_collection_progress_video (platform, video_id),
    CONSTRAINT fk_collection_progress_video
        FOREIGN KEY (platform, video_id)
        REFERENCES videos (platform, video_id),
    CONSTRAINT chk_collection_progress_status
        CHECK (status IN ('pending', 'partial', 'complete', 'blocked'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS comments (
    platform VARCHAR(32) NOT NULL,
    comment_id VARCHAR(64) NOT NULL,
    schema_version SMALLINT UNSIGNED NOT NULL,
    comment_id_kind VARCHAR(16) NOT NULL,
    video_id VARCHAR(64) NOT NULL,
    parent_comment_id VARCHAR(64) NULL,
    root_comment_id VARCHAR(64) NULL,
    comment_type VARCHAR(16) NOT NULL,
    author_role VARCHAR(16) NOT NULL,
    text LONGTEXT NOT NULL,
    like_count BIGINT UNSIGNED NOT NULL,
    reply_count BIGINT UNSIGNED NOT NULL,
    published_at DATETIME(6) NULL,
    published_label VARCHAR(255) NULL,
    first_collected_at DATETIME(6) NOT NULL,
    last_collected_at DATETIME(6) NOT NULL,
    source_url VARCHAR(2048) NOT NULL DEFAULT '',
    collection_batch VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, comment_id),
    KEY ix_comments_video (platform, video_id),
    KEY ix_comments_parent (platform, parent_comment_id),
    KEY ix_comments_root (platform, root_comment_id),
    KEY ix_comments_collected (last_collected_at),
    CONSTRAINT fk_comments_video
        FOREIGN KEY (platform, video_id)
        REFERENCES videos (platform, video_id),
    CONSTRAINT fk_comments_parent
        FOREIGN KEY (platform, parent_comment_id)
        REFERENCES comments (platform, comment_id),
    CONSTRAINT fk_comments_root
        FOREIGN KEY (platform, root_comment_id)
        REFERENCES comments (platform, comment_id),
    CONSTRAINT chk_comments_id_kind
        CHECK (comment_id_kind IN ('platform', 'synthetic')),
    CONSTRAINT chk_comments_type
        CHECK (comment_type IN ('root', 'reply')),
    CONSTRAINT chk_comments_author_role
        CHECK (author_role IN ('viewer', 'creator')),
    CONSTRAINT chk_comments_relationship
        CHECK (
            (comment_type = 'root' AND parent_comment_id IS NULL AND root_comment_id IS NULL)
            OR
            (comment_type = 'reply' AND parent_comment_id IS NOT NULL AND root_comment_id IS NOT NULL)
        ),
    CONSTRAINT chk_comments_collected_order
        CHECK (last_collected_at >= first_collected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS comment_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    comment_id VARCHAR(64) NOT NULL,
    collected_at DATETIME(6) NOT NULL,
    schema_version SMALLINT UNSIGNED NOT NULL,
    comment_id_kind VARCHAR(16) NOT NULL,
    video_id VARCHAR(64) NOT NULL,
    video_title LONGTEXT NOT NULL,
    video_url VARCHAR(2048) NOT NULL,
    parent_comment_id VARCHAR(64) NULL,
    root_comment_id VARCHAR(64) NULL,
    comment_type VARCHAR(16) NOT NULL,
    author_role VARCHAR(16) NOT NULL,
    text LONGTEXT NOT NULL,
    like_count BIGINT UNSIGNED NOT NULL,
    reply_count BIGINT UNSIGNED NOT NULL,
    published_at DATETIME(6) NULL,
    published_label VARCHAR(255) NULL,
    source_url VARCHAR(2048) NOT NULL DEFAULT '',
    collection_batch VARCHAR(255) NOT NULL DEFAULT '',
    record_json JSON NOT NULL,
    imported_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_comment_snapshots_identity (platform, comment_id, collected_at),
    KEY ix_comment_snapshots_video (platform, video_id, collected_at),
    CONSTRAINT fk_comment_snapshots_comment
        FOREIGN KEY (platform, comment_id)
        REFERENCES comments (platform, comment_id),
    CONSTRAINT chk_comment_snapshots_id_kind
        CHECK (comment_id_kind IN ('platform', 'synthetic')),
    CONSTRAINT chk_comment_snapshots_type
        CHECK (comment_type IN ('root', 'reply')),
    CONSTRAINT chk_comment_snapshots_author_role
        CHECK (author_role IN ('viewer', 'creator'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS comment_topics (
    platform VARCHAR(32) NOT NULL,
    comment_id VARCHAR(64) NOT NULL,
    topic_id VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, comment_id, topic_id),
    KEY ix_comment_topics_topic (topic_id),
    CONSTRAINT fk_comment_topics_comment
        FOREIGN KEY (platform, comment_id)
        REFERENCES comments (platform, comment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS comment_tags (
    platform VARCHAR(32) NOT NULL,
    comment_id VARCHAR(64) NOT NULL,
    tag VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, comment_id, tag),
    KEY ix_comment_tags_tag (tag),
    CONSTRAINT fk_comment_tags_comment
        FOREIGN KEY (platform, comment_id)
        REFERENCES comments (platform, comment_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS import_runs (
    import_run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_path VARCHAR(1024) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    target_manifest_path VARCHAR(1024) NOT NULL,
    target_manifest_sha256 CHAR(64) NOT NULL,
    progress_path VARCHAR(1024) NOT NULL,
    progress_sha256 CHAR(64) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    raw_comment_count BIGINT UNSIGNED NOT NULL,
    duplicate_source_count BIGINT UNSIGNED NOT NULL,
    stored_comment_count BIGINT UNSIGNED NOT NULL,
    inserted_snapshot_count BIGINT UNSIGNED NOT NULL,
    collection_count BIGINT UNSIGNED NOT NULL,
    target_video_count BIGINT UNSIGNED NOT NULL,
    progress_video_count BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (import_run_id),
    KEY ix_import_runs_completed (completed_at),
    KEY ix_import_runs_source_hash (source_sha256),
    CONSTRAINT chk_import_runs_status
        CHECK (status IN ('success', 'failed')),
    CONSTRAINT chk_import_runs_time_order
        CHECK (completed_at >= started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS creator_works (
    platform VARCHAR(32) NOT NULL,
    work_id VARCHAR(128) NOT NULL,
    work_id_kind VARCHAR(16) NOT NULL,
    title LONGTEXT NOT NULL,
    tags JSON NOT NULL,
    published_at DATETIME(6) NULL,
    content_type VARCHAR(128) NOT NULL DEFAULT '',
    audit_status VARCHAR(64) NOT NULL DEFAULT '',
    first_observed_at DATETIME(6) NOT NULL,
    last_observed_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (platform, work_id),
    KEY ix_creator_works_published (platform, published_at),
    CONSTRAINT chk_creator_works_id_kind
        CHECK (work_id_kind IN ('platform', 'synthetic')),
    CONSTRAINT chk_creator_works_observed_order
        CHECK (last_observed_at >= first_observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS work_metric_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    work_id VARCHAR(128) NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    view_count BIGINT UNSIGNED NOT NULL,
    like_count BIGINT UNSIGNED NOT NULL,
    share_count BIGINT UNSIGNED NOT NULL,
    comment_count BIGINT UNSIGNED NOT NULL,
    collect_count BIGINT UNSIGNED NOT NULL,
    profile_visit_count BIGINT UNSIGNED NOT NULL,
    follower_gain BIGINT UNSIGNED NOT NULL,
    completion_rate DECIMAL(12,8) NULL,
    five_second_completion_rate DECIMAL(12,8) NULL,
    cover_click_rate DECIMAL(12,8) NULL,
    two_second_bounce_rate DECIMAL(12,8) NULL,
    average_watch_seconds DECIMAL(14,6) NULL,
    source_file VARCHAR(255) NOT NULL DEFAULT '',
    source_sheet VARCHAR(255) NOT NULL DEFAULT '',
    record_json JSON NOT NULL,
    imported_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_work_metric_snapshots_identity
        (platform, work_id, observed_at),
    KEY ix_work_metric_snapshots_observed (platform, observed_at),
    CONSTRAINT fk_work_metric_snapshots_work
        FOREIGN KEY (platform, work_id)
        REFERENCES creator_works (platform, work_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS creator_profile_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    display_name VARCHAR(120) NOT NULL DEFAULT '',
    follower_count BIGINT UNSIGNED NOT NULL,
    following_count BIGINT UNSIGNED NOT NULL,
    total_like_count BIGINT UNSIGNED NOT NULL,
    work_count BIGINT UNSIGNED NOT NULL,
    record_json JSON NOT NULL,
    imported_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_creator_profile_snapshots_identity (platform, observed_at),
    KEY ix_creator_profile_snapshots_observed (platform, observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS audience_snapshots (
    snapshot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    dimension_name VARCHAR(64) NOT NULL,
    segment_name VARCHAR(120) NOT NULL,
    share_value DECIMAL(12,8) NOT NULL,
    sample_size BIGINT UNSIGNED NULL,
    record_json JSON NOT NULL,
    imported_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uq_audience_snapshots_identity
        (platform, observed_at, dimension_name, segment_name),
    KEY ix_audience_snapshots_observed (platform, observed_at),
    CONSTRAINT chk_audience_snapshots_share
        CHECK (share_value >= 0 AND share_value <= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
