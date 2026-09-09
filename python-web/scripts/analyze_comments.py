#!/usr/bin/env python3
"""Generate a baseline Markdown report from a creator comment archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.comment_data import (
    CommentDataError,
    deduplicate_records,
    load_records,
    validate_record_relations,
)


DEFAULT_INPUT: Path | None = None
DEFAULT_RULES: Path | None = None
DEFAULT_OUTPUT: Path | None = None

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@[\w.-]+")
CHINESE_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
LATIN_TERM_RE = re.compile(r"[a-z][a-z0-9.+-]{1,20}", re.IGNORECASE)
VISUAL_PLACEHOLDER_RE = re.compile(
    r"\[\[[^\[\]\r\n]{1,32}\]\]|\[图片表情\]"
)
QUESTION_REQUEST_RE = re.compile(
    r"(?:请问|求教|请教|想问|问一下|咨询一下|麻烦问)"
)
QUESTION_CLAUSE_RE = re.compile(
    r"(?:^|[\s，,。.!！?？;；：:])[^，,。.!！?？;；：:\r\n]{0,12}"
    r"(?:为什么|为啥|怎么|咋办|咋整|是否|是不是|会不会|能不能|可不可以|"
    r"有没有|什么原因|哪款|哪个|哪种|哪里)"
)
QUESTION_END_RE = re.compile(
    r"(?:吗|么|呢|怎么办|咋办|咋整|多少(?:公里|万公里|钱|年|个月|升|度|转|"
    r"码|迈)?)[\s。.!！…~～]*$"
)
NONQUESTION_CONTEXT_RE = re.compile(
    r"(?:不用怎么|不怎么|(?:再|在)怎么说|不知道能不能|随便[^，,。]{0,8}怎么|"
    r"怎么使用方便怎么来)"
)
STOP_TERMS = {
    "一个",
    "一些",
    "一下",
    "一直",
    "不是",
    "不能",
    "什么",
    "他们",
    "但是",
    "你们",
    "可以",
    "可能",
    "因为",
    "如何",
    "如果",
    "就是",
    "已经",
    "应该",
    "怎么",
    "感觉",
    "所以",
    "时候",
    "有没有",
    "然后",
    "现在",
    "这个",
    "还是",
    "那么",
    "那个",
    "问题",
    "非常",
    "为什么",
}
STOP_LATIN = {"http", "https", "www", "com", "douyin"}
BOUNDARY_STOP_CHARS = set("的一了呢吗啊吧呀嘛哦是在和都也就还我你他她它这那很太又要会能不没给让把被到上下来去")


def load_theme_rules(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CommentDataError(f"theme rule file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CommentDataError(f"invalid theme rule JSON: {path}: {exc.msg}") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CommentDataError("theme rules must be an object with version 1")
    themes = payload.get("themes")
    if not isinstance(themes, list):
        raise CommentDataError("theme rules must contain a themes array")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, theme in enumerate(themes):
        if not isinstance(theme, dict):
            raise CommentDataError(f"theme rule {index} must be an object")
        theme_id = theme.get("id")
        label = theme.get("label")
        keywords = theme.get("keywords")
        if not isinstance(theme_id, str) or not theme_id:
            raise CommentDataError(f"theme rule {index} has no id")
        if theme_id in seen:
            raise CommentDataError(f"duplicate theme rule id: {theme_id}")
        if not isinstance(label, str) or not label.strip():
            raise CommentDataError(f"theme rule {theme_id} has no label")
        if not isinstance(keywords, list) or not keywords or any(
            not isinstance(keyword, str) or not keyword.strip() for keyword in keywords
        ):
            raise CommentDataError(f"theme rule {theme_id} must have non-empty keywords")
        seen.add(theme_id)
        normalized.append(
            {
                "id": theme_id,
                "label": label.strip(),
                "keywords": [
                    unicodedata.normalize("NFKC", keyword).casefold()
                    for keyword in keywords
                ],
            }
        )
    return normalized


def normalized_for_search(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def strip_visual_placeholders(text: str) -> str:
    """Remove textualized emoji/image placeholders from analysis copies only."""
    return VISUAL_PLACEHOLDER_RE.sub(" ", text)


def infer_theme_ids(record: dict[str, Any], rules: list[dict[str, Any]]) -> set[str]:
    text = normalized_for_search(strip_visual_placeholders(record["text"]))
    inferred: set[str] = set()
    for rule in rules:
        if any(keyword in text for keyword in rule["keywords"]):
            inferred.add(rule["id"])
    return inferred


def is_question(text: str) -> bool:
    normalized = normalized_for_search(strip_visual_placeholders(text)).strip()
    if (
        "?" in normalized
        or "？" in normalized
        or bool(QUESTION_REQUEST_RE.search(normalized))
        or bool(QUESTION_END_RE.search(normalized))
    ):
        return True
    if NONQUESTION_CONTEXT_RE.search(normalized):
        return False
    return bool(QUESTION_CLAUSE_RE.search(normalized))


def markdown_cell(value: Any, *, limit: int | None = None) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return text


def compact_text(value: Any, *, limit: int) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: max(1, limit - 1)] + "…"
    return text


def markdown_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    materialized = [list(row) for row in rows]
    if not materialized:
        return ["_暂无数据。_"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(markdown_cell(value) for value in row) + " |"
        for row in materialized
    )
    return lines


def _candidate_terms(
    text: str, known_terms: set[str]
) -> Counter[str]:
    searchable = URL_RE.sub(
        " ", normalized_for_search(strip_visual_placeholders(text))
    )
    searchable = MENTION_RE.sub(" ", searchable)
    candidates: Counter[str] = Counter()

    for known in known_terms:
        count = searchable.count(known)
        if count:
            candidates[known] += count

    for token in LATIN_TERM_RE.findall(searchable):
        token = token.casefold()
        if token not in STOP_LATIN and token not in known_terms:
            candidates[token] += 1

    for run in CHINESE_RUN_RE.findall(searchable):
        upper = min(5, len(run))
        for width in range(2, upper + 1):
            for offset in range(0, len(run) - width + 1):
                term = run[offset : offset + width]
                if term in known_terms or term in STOP_TERMS:
                    continue
                if term[0] in BOUNDARY_STOP_CHARS or term[-1] in BOUNDARY_STOP_CHARS:
                    continue
                candidates[term] += 1
    return candidates


def collect_term_stats(
    records: list[dict[str, Any]], rules: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    known_terms = {
        keyword for rule in rules for keyword in rule["keywords"] if len(keyword) >= 2
    }
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"comments": 0, "occurrences": 0, "videos": set(), "known": False}
    )
    for record in records:
        candidates = _candidate_terms(record["text"], known_terms)
        for term, occurrences in candidates.items():
            stats[term]["comments"] += 1
            stats[term]["occurrences"] += occurrences
            stats[term]["videos"].add(record["video_id"])
            stats[term]["known"] = term in known_terms
    return stats


def top_terms(
    stats: dict[str, dict[str, Any]], *, limit: int, min_frequency: int
) -> list[tuple[str, dict[str, Any]]]:
    ranked = sorted(
        (
            (term, values)
            for term, values in stats.items()
            if values["comments"] >= min_frequency
        ),
        key=lambda item: (
            -item[1]["comments"],
            -item[1]["occurrences"],
            -int(item[1]["known"]),
            -len(item[0]),
            item[0],
        ),
    )
    selected: list[tuple[str, dict[str, Any]]] = []
    for term, values in ranked:
        if not values["known"] and any(
            term in selected_term
            and values["comments"] == selected_values["comments"]
            for selected_term, selected_values in selected
        ):
            continue
        selected.append((term, values))
        if len(selected) >= limit:
            break
    return selected


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "未知"
    return value.replace("T", " ").replace("+00:00", "Z")


def build_report(
    records: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    *,
    source_name: str,
    raw_count: int,
    duplicate_count: int,
    term_limit: int,
    question_limit: int,
    min_frequency: int,
    cohort_title: str | None = None,
    workspace_name: str = "Creator workspace",
    platform: str = "douyin",
) -> str:
    rule_labels = {rule["id"]: rule["label"] for rule in rules}
    theme_utterances: Counter[str] = Counter()
    theme_viewer_roots: Counter[str] = Counter()
    theme_viewer_root_questions: Counter[str] = Counter()
    theme_threads: dict[str, set[tuple[str, str]]] = defaultdict(set)
    theme_videos: dict[str, set[str]] = defaultdict(set)
    topic_comments: Counter[str] = Counter()
    topic_videos: dict[str, set[str]] = defaultdict(set)
    video_rows: dict[str, dict[str, Any]] = {}
    viewer_root_questions: list[tuple[dict[str, Any], set[str]]] = []
    all_viewer_question_count = 0
    viewer_roots: list[dict[str, Any]] = []

    for record in records:
        tags = infer_theme_ids(record, rules)
        is_viewer = record["author_role"] == "viewer"
        is_root = record["comment_type"] == "root"
        is_viewer_root = is_viewer and is_root
        question = is_viewer and is_question(record["text"])
        viewer_root_question = is_viewer_root and question
        if question:
            all_viewer_question_count += 1
        if is_viewer_root:
            viewer_roots.append(record)
        if viewer_root_question:
            viewer_root_questions.append((record, tags))

        for tag in tags:
            theme_utterances[tag] += 1
            theme_threads[tag].add(
                (record["video_id"], record["root_comment_id"] or record["comment_id"])
            )
            theme_videos[tag].add(record["video_id"])
            if is_viewer_root:
                theme_viewer_roots[tag] += 1
            if viewer_root_question:
                theme_viewer_root_questions[tag] += 1
        if is_viewer_root:
            for topic_id in record["topic_ids"]:
                topic_comments[topic_id] += 1
                topic_videos[topic_id].add(record["video_id"])

        video = video_rows.setdefault(
            record["video_id"],
            {
                "title": record["video_title"] or "（未记录标题）",
                "utterances": 0,
                "threads": 0,
                "viewer_roots": 0,
                "replies": 0,
                "creator_utterances": 0,
                "viewer_root_questions": 0,
                "viewer_root_likes": 0,
                "tags": Counter(),
            },
        )
        if record["video_title"]:
            video["title"] = record["video_title"]
        video["utterances"] += 1
        video["threads" if is_root else "replies"] += 1
        if record["author_role"] == "creator":
            video["creator_utterances"] += 1
        if is_viewer_root:
            video["viewer_roots"] += 1
            video["viewer_root_likes"] += record["like_count"]
            video["tags"].update(tags)
        if viewer_root_question:
            video["viewer_root_questions"] += 1

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    collected_values = [record["collected_at"] for record in records]
    published_values = [
        record["published_at"] for record in records if record["published_at"]
    ]
    synthetic_count = sum(
        record["comment_id_kind"] == "synthetic" for record in records
    )
    relative_only_count = sum(
        bool(record["published_label"]) and not record["published_at"]
        for record in records
    )
    thread_count = sum(record["comment_type"] == "root" for record in records)
    reply_count = sum(record["comment_type"] == "reply" for record in records)
    creator_utterance_count = sum(
        record["author_role"] == "creator" for record in records
    )
    declared_reply_count = sum(
        record["reply_count"]
        for record in records
        if record["comment_type"] == "root"
    )
    platform_label = "抖音" if platform == "douyin" else platform
    report_title = f"# {markdown_cell(workspace_name)} · {platform_label}评论分析"
    if cohort_title:
        report_title += f"：{markdown_cell(cohort_title)}"
    published_start = min(published_values) if published_values else None
    published_end = max(published_values) if published_values else None
    latest_collected = max(collected_values) if collected_values else None

    lines = [
        report_title,
        "",
        (
            "> 由 `scripts/analyze_comments.py` 自动生成。选题口径以观众一级评论为主；"
            "全部发言用于保留讨论上下文。主题和风险命中均为关键词候选，形成选题前"
            "仍需人工复核原评论语境。"
        ),
        "",
        "## 数据概览",
        "",
        f"- 数据源：`{markdown_cell(source_name)}`",
    ]
    if cohort_title:
        lines.append(
            f"- 样本范围：{markdown_cell(cohort_title)}（不代表账号全量评论）"
        )
    lines.extend(
        [
            f"- 生成时间（UTC）：{generated_at}",
            f"- 输入记录：{raw_count}",
            f"- 去重后全部发言：{len(records)}",
            f"- 合并的重复快照：{duplicate_count}",
            f"- 视频数：{len(video_rows)}",
            f"- 独立讨论线程：{thread_count}",
            f"- 观众一级评论（选题口径）：{len(viewer_roots)}",
            f"- 回复：{reply_count}",
            f"- 作者发言：{creator_utterance_count}",
            f"- 观众一级疑问表达：{len(viewer_root_questions)}",
            f"- 全部观众疑问表达（含楼中楼）：{all_viewer_question_count}",
            f"- 已采集回复 / 页面声明回复：{reply_count} / {declared_reply_count}",
            f"- 合成评论 ID：{synthetic_count}",
            f"- 仅有相对时间、无精确发布时间：{relative_only_count}",
            (
                f"- 评论发布时间范围：{_format_timestamp(published_start)} 至 "
                f"{_format_timestamp(published_end)}"
            ),
            f"- 最近采集时间：{_format_timestamp(latest_collected)}",
            "",
            "## 视频统计",
            "",
        ]
    )

    sorted_videos = sorted(
        video_rows.items(), key=lambda item: (-item[1]["viewer_roots"], item[0])
    )
    lines.extend(
        markdown_table(
            [
                "视频",
                "标题",
                "全部发言",
                "独立线程",
                "观众一级",
                "回复",
                "作者发言",
                "观众一级疑问",
                "观众一级获赞",
                "选题主题",
            ],
            (
                (
                    video_id,
                    compact_text(values["title"], limit=36),
                    values["utterances"],
                    values["threads"],
                    values["viewer_roots"],
                    values["replies"],
                    values["creator_utterances"],
                    values["viewer_root_questions"],
                    values["viewer_root_likes"],
                    "、".join(
                        rule_labels.get(tag, tag)
                        for tag, _ in values["tags"].most_common(3)
                    )
                    or "未归类",
                )
                for video_id, values in sorted_videos
            ),
        )
    )

    lines.extend(
        [
            "",
            "## 主题标签统计（观众一级评论为选题口径）",
            "",
            "每条评论可以命中多个主题，因此各主题占比之和可能超过 100%。“全部发言”包含楼中楼和作者发言，只用于观察讨论上下文。",
            "",
        ]
    )
    sorted_themes = sorted(
        theme_utterances,
        key=lambda tag: (-theme_viewer_roots[tag], -theme_utterances[tag], tag),
    )
    lines.extend(
        markdown_table(
            [
                "主题",
                "标签 ID",
                "观众一级",
                "选题占比",
                "独立线程",
                "全部发言",
                "涉及视频",
                "观众一级疑问",
            ],
            (
                (
                    rule_labels.get(tag, tag),
                    tag,
                    theme_viewer_roots[tag],
                    f"{theme_viewer_roots[tag] / len(viewer_roots):.1%}"
                    if viewer_roots
                    else "0.0%",
                    len(theme_threads[tag]),
                    theme_utterances[tag],
                    len(theme_videos[tag]),
                    theme_viewer_root_questions[tag],
                )
                for tag in sorted_themes
            ),
        )
    )
    lines.extend(
        [
            "",
            "> “报警与高风险症状（关键词候选，需复核）”只表示文本命中，不能据此判断车辆风险等级或故障原因。",
        ]
    )

    if topic_comments:
        lines.extend(["", "## Topic ID 统计（观众一级评论）", ""])
        lines.extend(
            markdown_table(
                ["Topic ID", "观众一级评论", "涉及视频"],
                (
                    (topic_id, topic_comments[topic_id], len(topic_videos[topic_id]))
                    for topic_id in sorted(
                        topic_comments,
                        key=lambda topic: (-topic_comments[topic], topic),
                    )
                ),
            )
        )

    term_stats = collect_term_stats(viewer_roots, rules)
    frequent_terms = top_terms(
        term_stats, limit=term_limit, min_frequency=min_frequency
    )
    lines.extend(["", "## 高频词与短语", ""])
    lines.append(
        "仅统计观众一级评论；分析副本已剥离 `[[捂脸]]`、`[图片表情]` 等视觉占位。中文结果由 2 至 5 字符短语与主题词匹配生成，适合发现信号，不等同于语言学分词。"
    )
    lines.append("")
    lines.extend(
        markdown_table(
            ["词 / 短语", "出现评论", "出现次数", "涉及视频", "规则词"],
            (
                (
                    term,
                    values["comments"],
                    values["occurrences"],
                    len(values["videos"]),
                    "是" if values["known"] else "否",
                )
                for term, values in frequent_terms
            ),
        )
    )

    question_theme_counts: Counter[str] = Counter()
    question_theme_videos: dict[str, set[str]] = defaultdict(set)
    question_theme_examples: dict[str, tuple[int, int, int, str]] = {}
    for record, tags in viewer_root_questions:
        effective_tags = tags or {"unclassified"}
        for tag in effective_tags:
            question_theme_counts[tag] += 1
            question_theme_videos[tag].add(record["video_id"])
            candidate = (
                record["like_count"] + record["reply_count"],
                record["reply_count"],
                record["like_count"],
                record["text"],
            )
            if candidate > question_theme_examples.get(tag, (-1, -1, -1, "")):
                question_theme_examples[tag] = candidate

    lines.extend(
        [
            "",
            "## 高频问题方向（观众一级评论）",
            "",
            "疑问表达采用保守的规则识别，仍可能包含反问，进入选题前需人工复核。",
            "",
        ]
    )
    lines.extend(
        markdown_table(
            ["问题方向", "问题数", "涉及视频", "代表性表达"],
            (
                (
                    rule_labels.get(tag, "未归类" if tag == "unclassified" else tag),
                    question_theme_counts[tag],
                    len(question_theme_videos[tag]),
                    compact_text(question_theme_examples[tag][3], limit=72),
                )
                for tag in sorted(
                    question_theme_counts,
                    key=lambda item: (-question_theme_counts[item], item),
                )
            ),
        )
    )

    ranked_questions = sorted(
        viewer_root_questions,
        key=lambda item: (
            -(item[0]["like_count"] + item[0]["reply_count"]),
            -item[0]["reply_count"],
            -item[0]["like_count"],
            item[0]["video_id"],
            item[0]["comment_id"],
        ),
    )[:question_limit]
    lines.extend(["", "## 高互动问题样本（观众一级评论）", ""])
    lines.extend(
        markdown_table(
            ["视频", "问题", "赞", "回复", "主题", "评论 ID"],
            (
                (
                    record["video_id"],
                    compact_text(record["text"], limit=88),
                    record["like_count"],
                    record["reply_count"],
                    "、".join(
                        rule_labels.get(tag, tag) for tag in sorted(tags)
                    )
                    or "未归类",
                    record["comment_id"],
                )
                for record, tags in ranked_questions
            ),
        )
    )

    lines.extend(
        [
            "",
            "## 人工复核清单",
            "",
            "- 优先复核观众一级评论中跨多个视频重复出现的问题。",
            "- 回看高互动问题的上下文与作者回复，区分真实症状、操作误区和故障个案。",
            "- 将确认后的结论映射到工作区 Topic；没有对应节点时再新增主题。",
            "- 形成选题前记录所依据的评论 ID，保留从结论到原始数据的追溯关系。",
            "",
        ]
    )
    return "\n".join(lines)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workspace-name", default="Creator workspace")
    parser.add_argument("--platform", default="douyin")
    parser.add_argument(
        "--cohort-title",
        help="optional sample/cohort label shown in the report title and scope note",
    )
    parser.add_argument("--term-limit", type=int, default=25)
    parser.add_argument("--question-limit", type=int, default=20)
    parser.add_argument(
        "--min-frequency",
        type=int,
        default=2,
        help="minimum number of comments containing a term (default: 2)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.input is None or args.rules is None or args.output is None:
        try:
            from admin_app.workspace import load_active_workspace

            workspace = load_active_workspace()
        except Exception as exc:
            print(f"workspace paths are required: {exc}", file=sys.stderr)
            return 2
        args.input = args.input or (workspace.comments_dir / "comments.jsonl")
        args.rules = args.rules or (workspace.comments_dir / "theme-rules.json")
        args.output = args.output or (workspace.reports_dir / "comment-insights.md")
        if args.workspace_name == "Creator workspace":
            args.workspace_name = workspace.display_name
        if args.platform == "douyin":
            args.platform = workspace.platform
    if args.term_limit < 1 or args.question_limit < 1 or args.min_frequency < 1:
        print("limits and minimum frequency must be positive integers", file=sys.stderr)
        return 2
    try:
        raw_records = load_records([args.input])
        records, duplicate_count = deduplicate_records(raw_records)
        validate_record_relations(records)
        rules = load_theme_rules(args.rules)
        report = build_report(
            records,
            rules,
            source_name=args.input.name,
            raw_count=len(raw_records),
            duplicate_count=duplicate_count,
            term_limit=args.term_limit,
            question_limit=args.question_limit,
            min_frequency=args.min_frequency,
            cohort_title=args.cohort_title,
            workspace_name=args.workspace_name,
            platform=args.platform,
        )
    except CommentDataError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    write_text_atomic(args.output, report)
    print(f"Analyzed comments: {len(records)}")
    print(f"Duplicate snapshots merged: {duplicate_count}")
    print(f"Report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
