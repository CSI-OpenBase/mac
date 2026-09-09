from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
THEME_RULES = REPOSITORY_ROOT / "tests" / "fixtures" / "theme-rules.json"
sys.path.insert(0, str(SCRIPTS_DIR))

from analyze_comments import (  # noqa: E402
    build_report,
    collect_term_stats,
    infer_theme_ids,
    is_question,
    load_theme_rules,
)
from comment_data import (  # noqa: E402
    CommentDataError,
    load_records,
    merge_record_sets,
    normalize_record,
    validate_record_relations,
    write_jsonl_atomic,
)


def raw_comment(**overrides):
    record = {
        "video_id": "video-1",
        "video_title": "低速重新给油",
        "comment_id": "comment-1",
        "comment_type": "root",
        "author_role": "viewer",
        "text": "低速轻踩油门会抖动，这正常吗？",
        "like_count": 3,
        "reply_count": 1,
        "published_at": "2026-09-05T12:00:00+08:00",
        "collected_at": "2026-09-06T12:00:00+08:00",
    }
    record.update(overrides)
    return record


class CommentDataTests(unittest.TestCase):
    def test_synthetic_id_is_stable_across_collection_snapshots(self):
        first = raw_comment(
            comment_id="", like_count=1, published_at=None, published_label="1天前"
        )
        second = raw_comment(
            comment_id="",
            like_count=8,
            published_at=None,
            published_label="2天前",
            collected_at="2026-09-07T12:00:00+08:00",
        )

        normalized_first = normalize_record(first)
        normalized_second = normalize_record(second)

        self.assertEqual(normalized_first["comment_id_kind"], "synthetic")
        self.assertEqual(normalized_first["comment_id"], normalized_second["comment_id"])

    def test_merge_keeps_newest_snapshot_and_unions_manual_metadata(self):
        first = normalize_record(raw_comment(manual_tags=["low-speed"]))
        second = normalize_record(
            raw_comment(
                like_count=9,
                reply_count=4,
                collected_at="2026-09-07T12:00:00+08:00",
                manual_tags=["judder"],
                topic_ids=["topic-004"],
            )
        )

        merged, stats = merge_record_sets([first], [second])

        self.assertEqual(stats["new_records"], 0)
        self.assertEqual(stats["updated_records"], 1)
        self.assertEqual(merged[0]["like_count"], 9)
        self.assertEqual(merged[0]["reply_count"], 4)
        self.assertEqual(merged[0]["manual_tags"], ["judder", "low-speed"])
        self.assertEqual(merged[0]["topic_ids"], ["topic-004"])

    def test_relation_validation_accepts_partial_reply_collection(self):
        root = normalize_record(raw_comment(reply_count=2))
        reply = normalize_record(
            raw_comment(
                comment_id="reply-1",
                comment_type="reply",
                parent_comment_id="comment-1",
                root_comment_id="comment-1",
                reply_count=0,
            )
        )

        validate_record_relations([root, reply])
        with self.assertRaisesRegex(CommentDataError, "only 1 are stored"):
            validate_record_relations(
                [root, reply], require_complete_reply_counts=True
            )

    def test_relation_validation_rejects_invalid_hierarchy(self):
        root = normalize_record(raw_comment(reply_count=1))
        cases = {
            "missing parent": normalize_record(
                raw_comment(
                    comment_id="reply-missing-parent",
                    comment_type="reply",
                    parent_comment_id=None,
                    root_comment_id="comment-1",
                    reply_count=0,
                )
            ),
            "missing root": normalize_record(
                raw_comment(
                    comment_id="reply-missing-root",
                    comment_type="reply",
                    parent_comment_id="comment-1",
                    root_comment_id=None,
                    reply_count=0,
                )
            ),
            "orphan": normalize_record(
                raw_comment(
                    comment_id="reply-orphan",
                    comment_type="reply",
                    parent_comment_id="absent",
                    root_comment_id="absent",
                    reply_count=0,
                )
            ),
            "cross video": normalize_record(
                raw_comment(
                    video_id="video-2",
                    comment_id="reply-cross-video",
                    comment_type="reply",
                    parent_comment_id="comment-1",
                    root_comment_id="comment-1",
                    reply_count=0,
                )
            ),
        }

        for label, reply in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(CommentDataError):
                    validate_record_relations([root, reply])

    def test_merge_rejects_more_replies_than_root_reports(self):
        root = normalize_record(raw_comment(reply_count=0))
        reply = normalize_record(
            raw_comment(
                comment_id="reply-1",
                comment_type="reply",
                parent_comment_id="comment-1",
                root_comment_id="comment-1",
                reply_count=0,
            )
        )

        with self.assertRaisesRegex(CommentDataError, "reports 0 replies"):
            merge_record_sets([], [root, reply])

    def test_relation_validation_rejects_non_root_thread_target(self):
        root = normalize_record(raw_comment(reply_count=2))
        first_reply = normalize_record(
            raw_comment(
                comment_id="reply-1",
                comment_type="reply",
                parent_comment_id="comment-1",
                root_comment_id="comment-1",
                reply_count=0,
            )
        )
        invalid_reply = normalize_record(
            raw_comment(
                comment_id="reply-2",
                comment_type="reply",
                parent_comment_id="reply-1",
                root_comment_id="reply-1",
                reply_count=0,
            )
        )

        with self.assertRaisesRegex(CommentDataError, "non-root comment reply-1"):
            validate_record_relations([root, first_reply, invalid_reply])

    def test_relation_validation_rejects_parent_cycle(self):
        root = normalize_record(raw_comment(reply_count=2))
        first_reply = normalize_record(
            raw_comment(
                comment_id="reply-1",
                comment_type="reply",
                parent_comment_id="reply-2",
                root_comment_id="comment-1",
                reply_count=0,
            )
        )
        second_reply = normalize_record(
            raw_comment(
                comment_id="reply-2",
                comment_type="reply",
                parent_comment_id="reply-1",
                root_comment_id="comment-1",
                reply_count=0,
            )
        )

        with self.assertRaisesRegex(CommentDataError, "parent cycle detected"):
            validate_record_relations(
                [root, first_reply, second_reply],
                require_complete_reply_counts=True,
            )

    def test_personal_identity_fields_are_rejected(self):
        with self.assertRaisesRegex(CommentDataError, "personal fields"):
            normalize_record(raw_comment(author_name="不应保存"))

    def test_relative_time_label_is_preserved_without_inventing_timestamp(self):
        record = normalize_record(raw_comment(published_at=None, published_label="3月前"))

        self.assertIsNone(record["published_at"])
        self.assertEqual(record["published_label"], "3月前")

    def test_relative_label_is_not_accepted_as_exact_timestamp(self):
        with self.assertRaisesRegex(CommentDataError, "valid ISO 8601"):
            normalize_record(raw_comment(published_at="3月前"))

    def test_analysis_ignores_manual_tags_as_themes(self):
        rules = load_theme_rules(THEME_RULES)
        record = normalize_record(
            raw_comment(text="普通反馈", manual_tags=["needs-review"])
        )

        self.assertEqual(infer_theme_ids(record, rules), set())

    def test_question_detection_is_conservative_about_embedded_words(self):
        self.assertTrue(is_question("请教一下，堵车应该怎么开"))
        self.assertTrue(is_question("低速顿挫正常吗[[捂脸]]"))
        self.assertTrue(is_question("为什么低速会降到一挡"))
        self.assertTrue(is_question("速腾L是怀档，这咋整"))
        self.assertTrue(is_question("第一次换变速箱油多少公里"))
        self.assertTrue(is_question("我21款为啥不锁二挡呀"))
        self.assertTrue(is_question("没有手动模式，也没有S挡咋办"))
        self.assertFalse(is_question("急长下坡不用怎么踩刹车就能控制车速"))
        self.assertFalse(is_question("M挡对新手不怎么友好"))
        self.assertFalse(is_question("机械上再怎么说都会有磨损"))
        self.assertFalse(is_question("我不知道能不能理解刚才那句话"))
        self.assertFalse(is_question("平路随便你怎么踩都没有"))

    def test_theme_inference_removes_visual_placeholders(self):
        rules = load_theme_rules(THEME_RULES)
        record = normalize_record(
            raw_comment(text="[[发抖]][[点头]][[冒烟]]普通反馈")
        )

        self.assertEqual(infer_theme_ids(record, rules), set())

    def test_term_analysis_removes_visual_placeholders(self):
        rules = load_theme_rules(THEME_RULES)
        record = normalize_record(
            raw_comment(text="[[捂脸]][[捂脸]] 双离合半联动")
        )

        stats = collect_term_stats([record], rules)

        self.assertNotIn("捂脸", stats)
        self.assertIn("半联动", stats)

    def test_report_uses_viewer_roots_as_selection_scope(self):
        root = normalize_record(
            raw_comment(text="普通反馈", reply_count=2, like_count=4)
        )
        viewer_reply = normalize_record(
            raw_comment(
                comment_id="reply-viewer",
                comment_type="reply",
                parent_comment_id="comment-1",
                root_comment_id="comment-1",
                text="堵车应该怎么开？",
                reply_count=0,
            )
        )
        creator_reply = normalize_record(
            raw_comment(
                comment_id="reply-creator",
                comment_type="reply",
                parent_comment_id="comment-1",
                root_comment_id="comment-1",
                author_role="creator",
                text="堵车建议保持车距。",
                reply_count=0,
            )
        )
        records = [root, viewer_reply, creator_reply]
        validate_record_relations(records, require_complete_reply_counts=True)
        rules = load_theme_rules(THEME_RULES)

        report = build_report(
            records,
            rules,
            source_name="comments.jsonl",
            raw_count=len(records),
            duplicate_count=0,
            term_limit=20,
            question_limit=20,
            min_frequency=1,
        )

        self.assertIn("去重后全部发言：3", report)
        self.assertIn("观众一级评论（选题口径）：1", report)
        self.assertIn("观众一级疑问表达：0", report)
        self.assertIn("全部观众疑问表达（含楼中楼）：1", report)
        self.assertIn("| 拥堵通勤 | traffic | 0 | 0.0% | 1 | 2 | 1 | 0 |", report)

    def test_jsonl_round_trip_and_report(self):
        records = [
            normalize_record(raw_comment()),
            normalize_record(
                raw_comment(
                    comment_id="reply-1",
                    parent_comment_id="comment-1",
                    root_comment_id="comment-1",
                    comment_type="reply",
                    author_role="creator",
                    text="先确认是否只在二挡极低速出现。",
                    like_count=1,
                    reply_count=0,
                )
            ),
            normalize_record(
                raw_comment(
                    video_id="video-2",
                    video_title="堵车怎么开",
                    comment_id="comment-2",
                    text="堵车时能不能一直用M挡？",
                    like_count=11,
                    reply_count=3,
                    topic_ids=["manual-mode"],
                )
            ),
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "comments.jsonl"
            write_jsonl_atomic(path, records)
            loaded = load_records([path])

        rules = load_theme_rules(THEME_RULES)
        report = build_report(
            loaded,
            rules,
            source_name="comments.jsonl",
            raw_count=len(loaded),
            duplicate_count=0,
            term_limit=20,
            question_limit=20,
            min_frequency=1,
        )

        self.assertIn("去重后全部发言：3", report)
        self.assertIn("观众一级疑问表达：2", report)
        self.assertIn("手动模式", report)
        self.assertIn("manual-mode", report)
        self.assertIn("作者发言", report)
        self.assertIn("堵车时能不能一直用M挡？", report)

    def test_command_line_incremental_import_and_analysis(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            first_batch = temporary_path / "first.jsonl"
            second_batch = temporary_path / "second.jsonl"
            store = temporary_path / "store.jsonl"
            report = temporary_path / "report.md"

            first_batch.write_text(
                json.dumps(raw_comment(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            second_batch.write_text(
                "\n".join(
                    [
                        json.dumps(
                            raw_comment(
                                like_count=10,
                                collected_at="2026-09-07T12:00:00+08:00",
                            ),
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            raw_comment(
                                video_id="video-2",
                                comment_id="comment-2",
                                text="堵车到底应该怎么开？",
                            ),
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            first_import = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS_DIR / "ingest_comments.py"),
                    str(first_batch),
                    "--store",
                    str(store),
                ],
                capture_output=True,
                check=False,
            )
            second_import = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS_DIR / "ingest_comments.py"),
                    str(second_batch),
                    "--store",
                    str(store),
                ],
                capture_output=True,
                check=False,
            )
            analysis = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS_DIR / "analyze_comments.py"),
                    "--input",
                    str(store),
                    "--rules",
                    str(THEME_RULES),
                    "--output",
                    str(report),
                    "--min-frequency",
                    "1",
                ],
                capture_output=True,
                check=False,
            )

            self.assertEqual(first_import.returncode, 0, first_import.stderr)
            self.assertEqual(second_import.returncode, 0, second_import.stderr)
            self.assertEqual(analysis.returncode, 0, analysis.stderr)
            stored = load_records([store])
            self.assertEqual(len(stored), 2)
            self.assertEqual(
                next(row for row in stored if row["comment_id"] == "comment-1")[
                    "like_count"
                ],
                10,
            )
            self.assertIn("去重后全部发言：2", report.read_text(encoding="utf-8"))

    def test_invalid_batch_does_not_change_existing_store(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            store = temporary_path / "store.jsonl"
            invalid_batch = temporary_path / "invalid.jsonl"
            write_jsonl_atomic(store, [normalize_record(raw_comment())])
            original_content = store.read_bytes()
            invalid_batch.write_text(
                json.dumps(raw_comment(author_name="不应保存"), ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS_DIR / "ingest_comments.py"),
                    str(invalid_batch),
                    "--store",
                    str(store),
                ],
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(store.read_bytes(), original_content)

    def test_ingest_normalizes_batches_next_to_the_canonical_store(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            comments_dir = Path(temporary_directory) / "comments"
            batches_dir = comments_dir / "batches"
            batches_dir.mkdir(parents=True)
            store = comments_dir / "store.jsonl"
            batch = batches_dir / "minimal.jsonl"
            batch.write_text(
                json.dumps(
                    {"video_id": "video-1", "text": "低速顿挫正常吗？"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS_DIR / "ingest_comments.py"),
                    str(batch),
                    "--store",
                    str(store),
                ],
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archived = load_records([batch])
            self.assertEqual(len(archived), 1)
            self.assertTrue(archived[0]["collected_at"].endswith("Z"))
            self.assertEqual(archived[0]["collection_batch"], "minimal")


if __name__ == "__main__":
    unittest.main()
