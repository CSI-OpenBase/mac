from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from import_comments_mysql import (  # noqa: E402
    COLLECTION_UPSERT,
    COLLECTION_VIDEO_UPSERT,
    COMMENT_UPSERT,
    DEFAULT_PASSWORD_ENV,
    EXPECTED_TABLE_COLLATION,
    REQUIRED_TABLES,
    SNAPSHOT_INSERT,
    ImportDataError,
    ImportPayload,
    accepted_current_keys,
    build_parser,
    fetch_existing_collection_video_mappings,
    load_import_payload,
    parent_first,
    prune_removed_target_state,
    resolve_password,
    split_sql_statements,
    to_mysql_datetime,
    validate_collection_video_mappings,
    validate_existing_comment_snapshots,
    validate_identifier,
)


class MySQLImportTests(unittest.TestCase):
    def test_main_rejects_import_paths_outside_active_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace_dir = root / "workspace"
            comments_dir = workspace_dir / "comments"
            comments_dir.mkdir(parents=True)
            workspace = SimpleNamespace(
                directory=workspace_dir,
                comments_dir=comments_dir,
                database_name="creator_one",
                slug="creator-one",
                platform="douyin",
            )
            outside = root / "outside.jsonl"

            with patch(
                "admin_app.workspace.load_active_workspace",
                return_value=workspace,
            ):
                import import_comments_mysql as import_module

                for option in ("--comments", "--targets", "--progress"):
                    with self.subTest(option=option):
                        self.assertEqual(import_module.main([option, str(outside)]), 2)

    def test_repository_sources_load_without_database_access(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            comments_dir = Path(temporary_directory)
            comments_path = comments_dir / "comments.jsonl"
            targets_path = comments_dir / "collection-targets.json"
            progress_path = comments_dir / "collection-progress.json"
            video_id = "7654321098765432109"
            comment = {
                "schema_version": 1,
                "platform": "douyin",
                "comment_id": "1111111111111111111",
                "comment_id_kind": "platform",
                "video_id": video_id,
                "video_title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{video_id}",
                "parent_comment_id": None,
                "root_comment_id": None,
                "comment_type": "root",
                "author_role": "viewer",
                "text": "合成导入测试",
                "like_count": 3,
                "reply_count": 0,
                "published_at": None,
                "published_label": "1天前",
                "collected_at": "2026-09-06T03:00:00Z",
                "topic_ids": [],
                "manual_tags": [],
                "source_url": "",
                "collection_batch": "fixture-batch",
            }
            comments_path.write_text(
                json.dumps(comment, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            targets_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope_id": "fixture-scope",
                        "scope_note": "合成 fixture",
                        "generated_at": "2026-09-06T03:00:00Z",
                        "target_video_count": 1,
                        "completed_video_count": 1,
                        "collections": [
                            {
                                "collection_id": "fixture-collection",
                                "name": "测试合集",
                                "episode_count": 1,
                            }
                        ],
                        "videos": [
                            {
                                "video_id": video_id,
                                "collection_id": "fixture-collection",
                                "collection_name": "测试合集",
                                "episode": 1,
                                "title": "测试视频",
                                "video_url": f"https://www.douyin.com/video/{video_id}",
                                "card_metric": "1",
                                "status": "complete",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            progress_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope_id": "fixture-scope",
                        "target_video_count": 1,
                        "completed_video_count": 1,
                        "coverage_ratio": 1,
                        "stored_record_count": 1,
                        "updated_at": "2026-09-06T03:00:00Z",
                        "videos": {
                            video_id: {
                                "title": "测试视频",
                                "url": f"https://www.douyin.com/video/{video_id}",
                                "status": "complete",
                                "visible_comment_count": 1,
                                "stored_record_count": 1,
                                "last_batch": "fixture-batch",
                                "last_collected_at": "2026-09-06T03:00:00Z",
                                "notes": "fixture",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload = load_import_payload(
                comments_path, targets_path, progress_path
            )

        self.assertEqual(len(payload.snapshots), 1)
        self.assertEqual(len(payload.comments), 1)
        self.assertEqual(payload.duplicate_source_count, 0)
        self.assertEqual(len(payload.targets["collections"]), 1)
        self.assertEqual(len(payload.targets["videos"]), 1)
        self.assertEqual(len(payload.progress["videos"]), 1)

    def test_later_sync_recovers_snapshots_from_all_archived_batches(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            comments_dir = Path(temporary_directory)
            batches_dir = comments_dir / "batches"
            batches_dir.mkdir()
            comments_path = comments_dir / "douyin-comments.jsonl"
            targets_path = comments_dir / "collection-targets.json"
            progress_path = comments_dir / "collection-progress.json"
            video_id = "7654321098765432109"
            comment_id = "1111111111111111111"

            def comment_snapshot(
                collected_at: str,
                text: str,
                like_count: int,
                collection_batch: str = "",
            ) -> dict[str, object]:
                return {
                    "schema_version": 1,
                    "platform": "douyin",
                    "comment_id": comment_id,
                    "comment_id_kind": "platform",
                    "video_id": video_id,
                    "video_title": "测试视频",
                    "video_url": f"https://www.douyin.com/video/{video_id}",
                    "parent_comment_id": None,
                    "root_comment_id": None,
                    "comment_type": "root",
                    "author_role": "viewer",
                    "text": text,
                    "like_count": like_count,
                    "reply_count": 0,
                    "published_at": "2026-09-06T01:00:00Z",
                    "published_label": None,
                    "collected_at": collected_at,
                    "topic_ids": [],
                    "manual_tags": [],
                    "source_url": "",
                    "collection_batch": collection_batch,
                }

            first = comment_snapshot(
                "2026-09-06T02:00:00Z", "第一次采集", 1
            )
            second = comment_snapshot(
                "2026-09-06T03:00:00Z", "第二次采集", 9, "second-batch"
            )
            canonical_second = {**second, "manual_tags": ["reviewed"]}
            orphan = {
                **first,
                "comment_id": "3333333333333333333",
                "text": "尚未并入总账",
            }
            for path, record in (
                (batches_dir / "first-batch.jsonl", first),
                (batches_dir / "first-batch-copy.jsonl", first),
                (batches_dir / "second-batch.jsonl", second),
                (batches_dir / "orphan-batch.jsonl", orphan),
                (comments_path, canonical_second),
            ):
                path.write_text(
                    json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
                )

            targets_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope_id": "test-scope",
                        "scope_note": "",
                        "generated_at": "2026-09-06T03:00:00Z",
                        "collections": [
                            {
                                "collection_id": "test-collection",
                                "name": "测试合集",
                                "episode_count": 1,
                            }
                        ],
                        "videos": [
                            {
                                "video_id": video_id,
                                "collection_id": "test-collection",
                                "collection_name": "测试合集",
                                "episode": 1,
                                "title": "测试视频",
                                "video_url": f"https://www.douyin.com/video/{video_id}",
                                "card_metric": "",
                                "status": "pending",
                            }
                        ],
                        "target_video_count": 1,
                        "completed_video_count": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            progress_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "scope_id": "test-scope",
                        "target_video_count": 1,
                        "completed_video_count": 0,
                        "stored_record_count": 0,
                        "updated_at": "2026-09-06T03:00:00Z",
                        "videos": {},
                    }
                ),
                encoding="utf-8",
            )

            payload = load_import_payload(
                comments_path, targets_path, progress_path
            )
            original_archive_hash = payload.source_sha256
            orphan["text"] = "未入总账批次发生变化"
            (batches_dir / "orphan-batch.jsonl").write_text(
                json.dumps(orphan, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            changed_payload = load_import_payload(
                comments_path, targets_path, progress_path
            )
            self.assertNotEqual(changed_payload.source_sha256, original_archive_hash)
            self.assertEqual(len(changed_payload.snapshots), 2)

            conflict = {
                **first,
                "text": "相同身份却内容冲突",
                "collection_batch": "first-batch",
            }
            (batches_dir / "conflict-batch.jsonl").write_text(
                json.dumps(conflict, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ImportDataError, "conflicting archived snapshot content"
            ):
                load_import_payload(comments_path, targets_path, progress_path)

        self.assertEqual(len(payload.comments), 1)
        self.assertEqual(payload.comments[0]["text"], "第二次采集")
        self.assertEqual(payload.comments[0]["manual_tags"], ["reviewed"])
        self.assertEqual(len(payload.snapshots), 2)
        snapshots = {row["collected_at"]: row for row in payload.snapshots}
        self.assertEqual(
            snapshots["2026-09-06T02:00:00Z"]["collection_batch"],
            "first-batch",
        )
        self.assertEqual(
            snapshots["2026-09-06T03:00:00Z"]["collection_batch"],
            "second-batch",
        )

    def test_schema_contains_all_required_tables(self):
        schema_path = REPOSITORY_ROOT / "admin_app" / "resources" / "mysql-schema.sql"
        sql = schema_path.read_text(encoding="utf-8")
        statements = split_sql_statements(sql)
        names = {
            match.group(1)
            for statement in statements
            if (match := re.search(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", statement))
        }
        creator_tables = {
            "creator_works",
            "work_metric_snapshots",
            "creator_profile_snapshots",
            "audience_snapshots",
        }
        expected_tables = set(REQUIRED_TABLES) | creator_tables

        self.assertEqual(len(statements), len(expected_tables))
        self.assertEqual(names, expected_tables)
        self.assertTrue(all("ENGINE=InnoDB" in statement for statement in statements))
        self.assertTrue(all("CHARSET=utf8mb4" in statement for statement in statements))
        self.assertTrue(
            all(f"COLLATE={EXPECTED_TABLE_COLLATION}" in statement for statement in statements)
        )

    def test_sql_splitter_ignores_comments_and_quoted_semicolons(self):
        sql = """
        -- ignored ; comment
        CREATE TABLE first_table (value VARCHAR(10) DEFAULT 'a;b');
        /* block ; comment */
        CREATE TABLE `second_table` (`semi;colon` INT);
        """

        statements = split_sql_statements(sql)

        self.assertEqual(len(statements), 2)
        self.assertIn("'a;b'", statements[0])
        self.assertIn("`semi;colon`", statements[1])

    def test_database_identifier_validation_blocks_sql_injection(self):
        self.assertEqual(validate_identifier("douyin_2026"), "douyin_2026")
        for invalid in ("douyin-prod", "1douyin", "douyin`; DROP DATABASE x", "", "a" * 65):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ImportDataError):
                    validate_identifier(invalid)

    def test_password_prefers_environment_and_is_not_a_cli_option(self):
        calls: list[str] = []
        password = resolve_password(
            {DEFAULT_PASSWORD_ENV: "temporary-secret"},
            lambda message: calls.append(message) or "prompt-secret",
        )
        option_strings = {
            option
            for action in build_parser()._actions
            for option in action.option_strings
        }

        self.assertEqual(password, "temporary-secret")
        self.assertEqual(calls, [])
        self.assertNotIn("--password", option_strings)

    def test_password_ignores_legacy_environment_names(self):
        environment = {
            "CSI_HUB_DB_PASSWORD": "hub-secret",
            "DOUYIN_DB_PASSWORD": "douyin-secret",
        }
        calls: list[str] = []

        self.assertEqual(
            resolve_password(
                environment,
                lambda message: calls.append(message) or "prompt-secret",
            ),
            "prompt-secret",
        )
        self.assertEqual(calls, ["MySQL password: "])
        with self.assertRaisesRegex(ImportDataError, "cannot be empty"):
            resolve_password(environment, lambda _: "")

        environment[DEFAULT_PASSWORD_ENV] = "openbase-secret"
        self.assertEqual(resolve_password(environment), "openbase-secret")

    def test_password_uses_hidden_prompt_and_rejects_empty_value(self):
        self.assertEqual(resolve_password({}, lambda _: "prompt-secret"), "prompt-secret")
        with self.assertRaisesRegex(ImportDataError, "cannot be empty"):
            resolve_password({}, lambda _: "")

    def test_timestamp_is_converted_to_naive_utc_for_mysql(self):
        converted = to_mysql_datetime("2026-09-06T16:00:00.123456+08:00")

        self.assertEqual(converted, datetime(2026, 9, 6, 8, 0, 0, 123456))
        self.assertIsNone(converted.tzinfo)

    def test_parent_first_uses_full_reply_chain(self):
        root = {"platform": "douyin", "comment_id": "root", "parent_comment_id": None}
        reply = {
            "platform": "douyin",
            "comment_id": "reply",
            "parent_comment_id": "root",
        }
        nested = {
            "platform": "douyin",
            "comment_id": "nested",
            "parent_comment_id": "reply",
        }

        ordered = parent_first([nested, reply, root])

        self.assertEqual([row["comment_id"] for row in ordered], ["root", "reply", "nested"])

    def test_parent_first_rejects_missing_parent_and_cycle(self):
        missing = {
            "platform": "douyin",
            "comment_id": "reply",
            "parent_comment_id": "absent",
        }
        with self.assertRaisesRegex(ImportDataError, "missing parent"):
            parent_first([missing])

        first = {"platform": "douyin", "comment_id": "a", "parent_comment_id": "b"}
        second = {"platform": "douyin", "comment_id": "b", "parent_comment_id": "a"}
        with self.assertRaisesRegex(ImportDataError, "cycle"):
            parent_first([first, second])

    def test_only_equal_or_newer_snapshot_replaces_current_state(self):
        old = {
            "platform": "douyin",
            "comment_id": "old",
            "video_id": "video-1",
            "collected_at": "2026-09-05T00:00:00Z",
        }
        equal = {
            "platform": "douyin",
            "comment_id": "equal",
            "video_id": "video-1",
            "collected_at": "2026-09-06T00:00:00Z",
        }
        new = {
            "platform": "douyin",
            "comment_id": "new",
            "video_id": "video-1",
            "collected_at": "2026-09-07T00:00:00Z",
        }
        existing = {
            ("douyin", "old"): {
                "video_id": "video-1",
                "last_collected_at": datetime(2026, 9, 6),
            },
            ("douyin", "equal"): {
                "video_id": "video-1",
                "last_collected_at": datetime(2026, 9, 6),
            },
        }

        accepted = accepted_current_keys([old, equal, new], existing)

        self.assertEqual(accepted, [("douyin", "equal"), ("douyin", "new")])

    def test_database_collision_across_videos_is_rejected(self):
        record = {
            "platform": "douyin",
            "comment_id": "same-id",
            "video_id": "video-new",
            "collected_at": "2026-09-07T00:00:00Z",
        }
        existing = {
            ("douyin", "same-id"): {
                "video_id": "video-old",
                "last_collected_at": datetime(2026, 9, 6),
            }
        }

        with self.assertRaisesRegex(ImportDataError, "collision across videos"):
            accepted_current_keys([record], existing)

    def test_database_snapshot_identity_rejects_different_observation(self):
        incoming = {
            "schema_version": 1,
            "platform": "douyin",
            "comment_id": "comment-1",
            "comment_id_kind": "platform",
            "collected_at": "2026-09-06T03:00:00Z",
            "video_id": "video-1",
            "video_title": "本地标题",
            "video_url": "https://www.douyin.com/video/7654321098765432109",
            "parent_comment_id": None,
            "root_comment_id": None,
            "comment_type": "root",
            "author_role": "viewer",
            "text": "本地内容",
            "like_count": 1,
            "reply_count": 0,
            "published_at": None,
            "published_label": "1天前",
            "topic_ids": [],
            "manual_tags": [],
            "source_url": "https://www.douyin.com/video/7654321098765432109",
            "collection_batch": "batch-1",
        }

        class FakeCursor:
            def __init__(self, field, value):
                self.field = field
                self.value = value

            def execute(self, _query, _parameters):
                return None

            def fetchall(self):
                return [
                    {
                        "platform": "douyin",
                        "comment_id": "comment-1",
                        "collected_at": datetime(2026, 9, 6, 3),
                        "record_json": json.dumps(
                            {**incoming, self.field: self.value},
                            ensure_ascii=False,
                        ),
                    }
                ]

        for field, value in (
            ("text", "数据库中的不同内容"),
            ("video_url", "https://www.douyin.com/video/7654321098765432110"),
            ("manual_tags", ["reviewed"]),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ImportDataError, "conflicting MySQL comment snapshot"
                ):
                    validate_existing_comment_snapshots(
                        FakeCursor(field, value), [incoming]
                    )

    def test_collection_mapping_rejects_both_unique_key_conflicts(self):
        existing = [
            {
                "platform": "douyin",
                "collection_id": "collection-1",
                "video_id": "video-old",
                "episode": 3,
            }
        ]
        changed_video = [
            {
                "platform": "douyin",
                "collection_id": "collection-1",
                "video_id": "video-new",
                "episode": 3,
            }
        ]
        changed_episode = [
            {
                "platform": "douyin",
                "collection_id": "collection-1",
                "video_id": "video-old",
                "episode": 4,
            }
        ]

        with self.assertRaisesRegex(ImportDataError, "episode 3 is stored as video"):
            validate_collection_video_mappings(changed_video, existing)
        with self.assertRaisesRegex(ImportDataError, "stored as episode 3"):
            validate_collection_video_mappings(changed_episode, existing)
        validate_collection_video_mappings(existing, existing)

    def test_collection_mapping_query_uses_bound_parameters(self):
        class FakeCursor:
            def __init__(self):
                self.query = ""
                self.parameters = []

            def execute(self, query, parameters):
                self.query = query
                self.parameters = parameters

            def fetchall(self):
                return []

        cursor = FakeCursor()
        incoming = [
            {
                "platform": "douyin",
                "collection_id": "collection-1",
                "video_id": "video-1",
                "episode": 1,
            }
        ]

        rows = fetch_existing_collection_video_mappings(cursor, incoming)

        self.assertEqual(rows, [])
        self.assertIn("collection_id IN (%s)", cursor.query)
        self.assertEqual(cursor.parameters, ["douyin", "collection-1"])

    def test_newer_manifest_prunes_rows_missing_from_canonical_files(self):
        payload = ImportPayload(
            comments=[],
            snapshots=[],
            duplicate_source_count=0,
            targets={
                "scope_id": "scope-1",
                "collections": [{"collection_id": "keep"}],
                "videos": [{"collection_id": "keep", "video_id": "video-1"}],
            },
            progress={"videos": {"video-1": {}}},
            source_sha256="",
            targets_sha256="",
            progress_sha256="",
        )

        class FakeCursor:
            def __init__(self):
                self.rows = []
                self.deletes = []

            def execute(self, query, _parameters):
                if "MAX(manifest_generated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 6)}]
                elif "SELECT c.source_scope_id" in query:
                    self.rows = [
                        {
                            "source_scope_id": "scope-1",
                            "collection_id": "keep",
                            "video_id": "video-1",
                        },
                        {
                            "source_scope_id": "scope-1",
                            "collection_id": "remove",
                            "video_id": "video-2",
                        },
                    ]
                elif query.startswith("SELECT source_scope_id"):
                    self.rows = [
                        {"source_scope_id": "scope-1", "collection_id": "keep"},
                        {"source_scope_id": "scope-1", "collection_id": "remove"},
                    ]
                elif "MAX(source_updated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 6)}]
                elif query.startswith("SELECT scope_id"):
                    self.rows = [
                        {"scope_id": "scope-1", "video_id": "video-1"},
                        {"scope_id": "scope-1", "video_id": "video-2"},
                    ]
                else:
                    raise AssertionError(query)

            def fetchone(self):
                return self.rows[0]

            def fetchall(self):
                return self.rows

            def executemany(self, query, rows):
                values = list(rows)
                self.deletes.append((query, values))
                return len(values)

        cursor = FakeCursor()
        removed = prune_removed_target_state(
            cursor,
            payload=payload,
            manifest_generated_at=datetime(2026, 9, 7),
            progress_updated_at=datetime(2026, 9, 7),
        )

        self.assertEqual(
            removed,
            {"collections": 1, "collection_videos": 1, "progress": 1},
        )
        self.assertEqual(len(cursor.deletes), 3)
        self.assertTrue(all("video-2" in str(rows) or "remove" in str(rows) for _, rows in cursor.deletes))

    def test_new_scope_prunes_all_older_scope_state_in_dependency_order(self):
        payload = ImportPayload(
            comments=[],
            snapshots=[],
            duplicate_source_count=0,
            targets={
                "scope_id": "scope-new",
                "collections": [{"collection_id": "new-collection"}],
                "videos": [
                    {"collection_id": "new-collection", "video_id": "new-video"}
                ],
            },
            progress={"videos": {"new-video": {}}},
            source_sha256="",
            targets_sha256="",
            progress_sha256="",
        )

        class FakeCursor:
            def __init__(self):
                self.rows = []
                self.deletes = []

            def execute(self, query, _parameters):
                if "MAX(manifest_generated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 6)}]
                elif "MAX(source_updated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 6)}]
                elif query.startswith("SELECT scope_id"):
                    self.rows = [
                        {"scope_id": "scope-old", "video_id": "old-video"},
                        {"scope_id": "scope-new", "video_id": "new-video"},
                    ]
                elif "SELECT c.source_scope_id" in query:
                    self.rows = [
                        {
                            "source_scope_id": "scope-old",
                            "collection_id": "old-collection",
                            "video_id": "old-video",
                        },
                        {
                            "source_scope_id": "scope-new",
                            "collection_id": "new-collection",
                            "video_id": "new-video",
                        },
                    ]
                elif query.startswith("SELECT source_scope_id"):
                    self.rows = [
                        {
                            "source_scope_id": "scope-old",
                            "collection_id": "old-collection",
                        },
                        {
                            "source_scope_id": "scope-new",
                            "collection_id": "new-collection",
                        },
                    ]
                else:
                    raise AssertionError(query)

            def fetchone(self):
                return self.rows[0]

            def fetchall(self):
                return self.rows

            def executemany(self, query, rows):
                values = list(rows)
                self.deletes.append((query, values))
                return len(values)

        cursor = FakeCursor()
        removed = prune_removed_target_state(
            cursor,
            payload=payload,
            manifest_generated_at=datetime(2026, 9, 7),
            progress_updated_at=datetime(2026, 9, 7),
        )

        self.assertEqual(
            removed,
            {"collections": 1, "collection_videos": 1, "progress": 1},
        )
        self.assertEqual(
            [
                "progress"
                if "collection_progress" in query
                else "collection_videos"
                if "collection_videos" in query
                else "collections"
                for query, _rows in cursor.deletes
            ],
            ["progress", "collection_videos", "collections"],
        )
        self.assertEqual(cursor.deletes[0][1], [("douyin", "scope-old", "old-video")])
        self.assertEqual(
            cursor.deletes[1][1],
            [("douyin", "old-collection", "old-video")],
        )
        self.assertEqual(
            cursor.deletes[2][1],
            [("douyin", "scope-old", "old-collection")],
        )

    def test_older_scope_cannot_prune_newer_global_target_state(self):
        payload = ImportPayload(
            comments=[],
            snapshots=[],
            duplicate_source_count=0,
            targets={"scope_id": "scope-old", "collections": [], "videos": []},
            progress={"videos": {}},
            source_sha256="",
            targets_sha256="",
            progress_sha256="",
        )

        class FakeCursor:
            def __init__(self):
                self.rows = []
                self.deletes = []

            def execute(self, query, _parameters):
                if "MAX(manifest_generated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 8)}]
                elif "MAX(source_updated_at)" in query:
                    self.rows = [{"latest_at": datetime(2026, 9, 8)}]
                else:
                    raise AssertionError(f"older state must not enumerate rows: {query}")

            def fetchone(self):
                return self.rows[0]

            def executemany(self, query, rows):
                self.deletes.append((query, list(rows)))
                return 0

        cursor = FakeCursor()
        removed = prune_removed_target_state(
            cursor,
            payload=payload,
            manifest_generated_at=datetime(2026, 9, 7),
            progress_updated_at=datetime(2026, 9, 7),
        )

        self.assertEqual(
            removed,
            {"collections": 0, "collection_videos": 0, "progress": 0},
        )
        self.assertEqual(cursor.deletes, [])

    def test_sql_encodes_idempotency_and_current_state_rules(self):
        self.assertIn("INSERT INTO comment_snapshots", SNAPSHOT_INSERT)
        self.assertNotIn("INSERT IGNORE", SNAPSHOT_INSERT)
        self.assertIn("ON DUPLICATE KEY UPDATE snapshot_id = snapshot_id", SNAPSHOT_INSERT)
        self.assertIn("VALUES(last_collected_at) >= last_collected_at", COMMENT_UPSERT)
        self.assertIn(
            "first_collected_at = LEAST(first_collected_at, VALUES(first_collected_at))",
            COMMENT_UPSERT,
        )
        self.assertLess(
            COMMENT_UPSERT.index("schema_version = IF"),
            COMMENT_UPSERT.index("last_collected_at = GREATEST"),
        )

    def test_manifest_current_state_is_not_replaced_by_an_older_manifest(self):
        for statement in (COLLECTION_UPSERT, COLLECTION_VIDEO_UPSERT):
            with self.subTest(statement=statement.splitlines()[1]):
                self.assertIn(
                    "VALUES(manifest_generated_at) >= manifest_generated_at", statement
                )
                self.assertIn(
                    "manifest_generated_at = GREATEST(", statement
                )


if __name__ == "__main__":
    unittest.main()
