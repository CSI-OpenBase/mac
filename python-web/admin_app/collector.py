"""Cautious Douyin comment capture for the local administration worker.

Only normalized, anonymous comment records are written. Raw HTTP responses are
never persisted, and browser state stays in the repository's ignored runtime
directory. A capture is reported as complete only when all counts match, or
partial when every page was exhausted but Douyin no longer exposes every
declared comment.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from .archive_lock import write_collection_state
from .runtime_paths import default_runtime_root, require_safe_runtime_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from scripts.comment_data import (
    CommentDataError,
    deduplicate_records,
    load_records,
    normalize_record,
    validate_record_relations,
    write_jsonl_atomic,
)


COMMENT_ENDPOINT_RE = re.compile(
    r"/aweme/v1/web/comment/list(?P<reply>/reply)?/?$", re.IGNORECASE
)
VIDEO_ID_RE = re.compile(r"^[0-9]{8,32}$")
EXPAND_TEXT_RE = re.compile(
    r"^\s*(?:展开\s*(?:更多(?:\s*回复)?|全部\s*回复|\d+\s*条\s*回复|回复)"
    r"|查看\s*(?:更多|全部)\s*回复|更多\s*回复)\s*$"
)
CHALLENGE_TEXTS = (
    "请完成下列验证",
    "请完成验证",
    "检测到异常",
    "访问过于频繁",
    "网络环境存在风险",
)
LOGIN_TEXTS = (
    "登录后查看评论",
    "登录后即可查看评论",
)
BLOCKER_SCOPE_SELECTOR = "dialog, [role='dialog'], [role='alert'], form"
ZERO_IDS = {"", "0", "-1", "None", "null"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def default_browser_profile_dir() -> Path:
    return default_runtime_root(REPOSITORY_ROOT) / "sessions" / "default"


def _clean_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if value in ZERO_IDS else value


def _first_id(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _clean_id(source.get(key))
        if value:
            return value
    return None


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if candidate.isdigit():
            return int(candidate)
    return 0


def _is_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, float):
        return value >= 0 and value.is_integer()
    if isinstance(value, str):
        return value.strip().replace(",", "").isdigit()
    return False


def _timestamp(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _identity_tokens(user: Any) -> frozenset[tuple[str, str]]:
    """Return transient comparison tokens; callers must never serialize them."""
    if not isinstance(user, Mapping):
        return frozenset()
    namespaces = {
        "uid": "uid",
        "user_id": "uid",
        "sec_uid": "sec_uid",
        "short_id": "short_id",
        "unique_id": "unique_id",
    }
    tokens = {
        (namespace, value)
        for key, namespace in namespaces.items()
        if (value := _clean_id(user.get(key)))
    }
    return frozenset(tokens)


def _has_more_value(payload: Mapping[str, Any]) -> bool | None:
    if "has_more" not in payload:
        return None
    value = payload.get("has_more")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no"}:
            return False
        if normalized in {"1", "true", "yes"}:
            return True
    return None


def _response_status_ok(payload: Mapping[str, Any]) -> bool:
    status = payload.get("status_code", 0)
    return status in (0, "0", None)


def _comment_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    value = payload.get("comments")
    if value is None and "comment_list" in payload:
        value = payload.get("comment_list")
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, Mapping)]


@dataclass(slots=True)
class _CapturedComment:
    record: dict[str, Any]
    author_tokens: frozenset[tuple[str, str]] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CollectionResult:
    status: str
    message: str
    video_id: str
    records: tuple[dict[str, Any], ...]
    batch_path: Path | None
    diagnostics: Mapping[str, Any]

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked"


@dataclass(frozen=True, slots=True)
class CaptureAssessment:
    status: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    records: tuple[dict[str, Any], ...]

    @property
    def is_finished(self) -> bool:
        return self.status != "blocked"


class ResponseAccumulator:
    """Parse known public comment responses without retaining personal fields."""

    def __init__(
        self,
        *,
        video_id: str,
        video_url: str,
        video_title: str = "",
        collected_at: str | None = None,
        batch_name: str = "pending",
    ) -> None:
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("video_id must contain 8-32 digits")
        self.video_id = video_id
        self.video_url = video_url
        self.video_title = video_title.strip()
        self.collected_at = collected_at or utc_now()
        self.batch_name = batch_name
        self._comments: dict[str, _CapturedComment] = {}
        self._creator_tokens: set[tuple[str, str]] = set()
        self._root_expected_replies: dict[str, int] = {}
        self._root_response_seen = False
        self._root_terminal_seen = False
        self._reply_terminal_roots: set[str] = set()
        self._root_pages = 0
        self._reply_pages = 0
        self._reported_total: int | None = None
        self._errors: list[str] = []

    @property
    def saw_comment_response(self) -> bool:
        return self._root_response_seen or self._reply_pages > 0

    def set_page_title(self, title: str) -> None:
        title = title.strip()
        if title and not self.video_title:
            self.video_title = re.sub(r"\s*[-|_]\s*抖音\s*$", "", title).strip()

    def add_error(self, message: str) -> None:
        message = message.strip()
        if message and message not in self._errors:
            self._errors.append(message)

    def consume(self, response_url: str, payload: Any) -> bool:
        """Consume one decoded response. Return whether it was relevant."""
        if not isinstance(payload, Mapping):
            return False
        parsed = urlparse(response_url)
        endpoint_match = COMMENT_ENDPOINT_RE.search(parsed.path)
        if endpoint_match:
            self._consume_comment_page(
                payload,
                is_reply=bool(endpoint_match.group("reply")),
                query=parse_qs(parsed.query),
            )
            return True
        if "aweme/detail" in parsed.path or "aweme/post" in parsed.path:
            self._consume_video_metadata(payload)
            return True
        return False

    def _consume_video_metadata(self, payload: Mapping[str, Any]) -> None:
        candidates: list[Mapping[str, Any]] = []
        detail = payload.get("aweme_detail")
        if isinstance(detail, Mapping):
            candidates.append(detail)
        for key in ("aweme_list", "item_list"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, Mapping))
        for item in candidates:
            item_id = _first_id(item, "aweme_id", "item_id", "group_id")
            if item_id and item_id != self.video_id:
                continue
            self._creator_tokens.update(_identity_tokens(item.get("author")))
            description = item.get("desc") or item.get("description")
            if isinstance(description, str) and description.strip():
                self.video_title = description.strip()

    def _consume_comment_page(
        self,
        payload: Mapping[str, Any],
        *,
        is_reply: bool,
        query: Mapping[str, Sequence[str]],
    ) -> None:
        for key in ("aweme_id", "item_id", "group_id"):
            values = query.get(key)
            response_video_id = _clean_id(values[0]) if values else None
            if response_video_id and response_video_id != self.video_id:
                return
        if not _response_status_ok(payload):
            self.add_error("Douyin returned a non-zero status for a comment response")
            return
        comments = _comment_items(payload)
        if comments is None:
            self.add_error("The comment response shape changed: comments array is missing")
            return
        original_comments = payload.get("comments", payload.get("comment_list"))
        if isinstance(original_comments, list) and len(comments) != len(original_comments):
            self.add_error("The comment response contained a non-object row")

        if not is_reply and "total" in payload:
            total_value = payload.get("total")
            if isinstance(total_value, bool):
                self.add_error("The root-comment total was not numeric")
            else:
                try:
                    reported_total = int(total_value)
                except (TypeError, ValueError):
                    self.add_error("The root-comment total was not numeric")
                else:
                    if reported_total < 0:
                        self.add_error("The root-comment total was negative")
                    else:
                        self._reported_total = max(
                            self._reported_total or 0, reported_total
                        )

        endpoint_root_id = None
        if is_reply:
            for key in ("comment_id", "root_comment_id", "cid"):
                values = query.get(key)
                if values and (endpoint_root_id := _clean_id(values[0])):
                    break
            self._reply_pages += 1
        else:
            self._root_response_seen = True
            self._root_pages += 1

        for item in comments:
            self._consume_comment(
                item,
                force_reply=is_reply,
                endpoint_root_id=endpoint_root_id,
            )

        has_more = _has_more_value(payload)
        if has_more is False:
            if is_reply:
                root_id = endpoint_root_id
                if root_id is None and comments:
                    root_id = _first_id(comments[0], "root_comment_id", "reply_id")
                if root_id:
                    self._reply_terminal_roots.add(root_id)
                else:
                    self.add_error(
                        "A terminal reply page did not identify its root comment"
                    )
            else:
                self._root_terminal_seen = True

    def _consume_comment(
        self,
        raw: Mapping[str, Any],
        *,
        force_reply: bool,
        endpoint_root_id: str | None,
    ) -> None:
        row_video_id = _first_id(raw, "aweme_id", "item_id", "group_id")
        if row_video_id and row_video_id != self.video_id:
            self.add_error(
                f"A comment response mixed target video {self.video_id} with {row_video_id}"
            )
            return
        comment_id = _first_id(raw, "cid", "comment_id", "comment_id_str")
        text = raw.get("text")
        if not isinstance(text, str):
            text = raw.get("content")
        has_media = any(
            bool(raw.get(key))
            for key in (
                "image_list",
                "comment_image",
                "sticker",
                "sticker_info",
            )
        )
        if isinstance(text, str) and not text.strip() and has_media:
            text = "[[图片]]"
        if not comment_id or not isinstance(text, str) or not text.strip():
            self.add_error("A comment response contained a row without an ID or text")
            return

        reply_id = _first_id(raw, "root_comment_id", "reply_id")
        reply_to_id = _first_id(
            raw,
            "reply_to_reply_id",
            "reply_to_comment_id",
            "parent_comment_id",
        )
        is_reply = force_reply or bool(endpoint_root_id or reply_id or reply_to_id)
        root_id = (endpoint_root_id or reply_id) if is_reply else None
        parent_id = (reply_to_id or root_id) if is_reply else None
        if is_reply and not root_id:
            self.add_error(f"Reply {comment_id} did not identify a root comment")
            return

        raw_reply_count = raw.get(
            "reply_comment_total", raw.get("reply_count", 0)
        )
        if not is_reply and not _is_count(raw_reply_count):
            self.add_error(f"Root {comment_id} had an invalid declared reply count")
        raw_like_count = raw.get("digg_count", raw.get("like_count", 0))
        if not _is_count(raw_like_count):
            self.add_error(f"Comment {comment_id} had an invalid like count")
        record = {
            "schema_version": 1,
            "platform": "douyin",
            "comment_id": comment_id,
            "comment_id_kind": "platform",
            "video_id": self.video_id,
            "video_title": self.video_title,
            "video_url": self.video_url,
            "parent_comment_id": parent_id,
            "root_comment_id": root_id,
            "comment_type": "reply" if is_reply else "root",
            "author_role": "viewer",
            "text": text.strip(),
            "like_count": _safe_count(raw_like_count),
            "reply_count": _safe_count(raw_reply_count),
            "published_at": _timestamp(
                raw.get("create_time", raw.get("create_timestamp"))
            ),
            # Exact API timestamps are preferred; location-like display labels are
            # intentionally not copied.
            "published_label": None,
            "collected_at": self.collected_at,
            "topic_ids": [],
            "manual_tags": [],
            "source_url": "",
            "collection_batch": self.batch_name,
        }
        author_tokens = _identity_tokens(raw.get("user") or raw.get("author"))
        if not author_tokens:
            self.add_error(
                f"Comment {comment_id} had no transient author identity for role classification"
            )
        previous = self._comments.get(comment_id)
        if previous is not None and (
            previous.record["comment_type"] != record["comment_type"]
            or previous.record["root_comment_id"] != record["root_comment_id"]
        ):
            self.add_error(
                f"Comment {comment_id} appeared with conflicting relationship data"
            )
        self._comments[comment_id] = _CapturedComment(record, author_tokens)
        if not is_reply:
            if "reply_comment_total" not in raw and "reply_count" not in raw:
                self.add_error(
                    f"Root {comment_id} did not include a declared reply count"
                )
            self._root_expected_replies[comment_id] = record["reply_count"]

        embedded = raw.get("reply_comment") or raw.get("reply_comments")
        if isinstance(embedded, list):
            for reply in embedded:
                if isinstance(reply, Mapping):
                    self._consume_comment(
                        reply,
                        force_reply=True,
                        endpoint_root_id=comment_id if not is_reply else root_id,
                    )

    def _materialize(
        self,
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        errors = list(self._errors)
        warnings: list[str] = []
        confirmed_empty = bool(
            not self._comments
            and self._root_terminal_seen
            and self._reported_total == 0
        )
        if not self._creator_tokens and not confirmed_empty:
            errors.append(
                "The video author identity was not observed, so creator replies "
                "cannot be classified reliably"
            )
            return [], list(dict.fromkeys(errors)), warnings
        normalized: list[dict[str, Any]] = []
        for captured in self._comments.values():
            record = dict(captured.record)
            if not captured.author_tokens:
                # The canonical schema has no "unknown" author role. Dropping an
                # unclassifiable row is safer than poisoning the long-term store.
                continue
            if self.video_title:
                record["video_title"] = self.video_title
            if captured.author_tokens and captured.author_tokens & self._creator_tokens:
                record["author_role"] = "creator"
            try:
                normalized.append(normalize_record(record))
            except CommentDataError as exc:
                errors.append(f"Comment {record.get('comment_id', '?')} was rejected: {exc}")

        normalized, _ = deduplicate_records(normalized)
        ids = {row["comment_id"] for row in normalized}
        valid: list[dict[str, Any]] = []
        for row in normalized:
            if row["comment_type"] == "reply":
                if row["root_comment_id"] not in ids:
                    errors.append(
                        f"Reply {row['comment_id']} references a root comment that "
                        "was not captured"
                    )
                    continue
                if row["parent_comment_id"] not in ids:
                    warnings.append(
                        f"Reply {row['comment_id']} references an unavailable parent "
                        "and was excluded"
                    )
                    continue
            valid.append(row)
        try:
            validate_record_relations(valid)
        except CommentDataError as exc:
            errors.append(str(exc))
        return (
            valid,
            list(dict.fromkeys(errors)),
            list(dict.fromkeys(warnings)),
        )

    def assessment(self) -> CaptureAssessment:
        records, blockers, warnings = self._materialize()
        if not self._root_response_seen:
            blockers.append("No recognized root-comment response was observed")
        elif not self._root_terminal_seen:
            blockers.append("Root-comment pagination did not reach has_more=false")

        replies_by_root: dict[str, int] = {}
        for row in records:
            if row["comment_type"] == "reply":
                root_id = row["root_comment_id"]
                replies_by_root[root_id] = replies_by_root.get(root_id, 0) + 1
        for root_id, expected in sorted(self._root_expected_replies.items()):
            captured = replies_by_root.get(root_id, 0)
            if captured != expected:
                terminal = root_id in self._reply_terminal_roots
                message = (
                    f"Root {root_id} declares {expected} replies but {captured} were "
                    "captured ("
                    + (
                        "terminal reply page observed"
                        if terminal
                        else "reply pagination not closed"
                    )
                    + ")"
                )
                (warnings if terminal else blockers).append(message)

        root_count = sum(row["comment_type"] == "root" for row in records)
        if self._reported_total is not None and self._reported_total not in {
            root_count,
            len(records),
        }:
            message = (
                f"Douyin reported total {self._reported_total}, but the capture has "
                f"{root_count} roots and {len(records)} total records"
            )
            reply_pagination_closed = all(
                replies_by_root.get(root_id, 0) == expected
                or root_id in self._reply_terminal_roots
                for root_id, expected in self._root_expected_replies.items()
            )
            if self._root_terminal_seen and reply_pagination_closed:
                warnings.append(message)
            else:
                blockers.append(message)

        blockers = list(dict.fromkeys(blockers))
        warnings = list(dict.fromkeys(warnings))
        status = "blocked" if blockers else ("partial" if warnings else "complete")
        return CaptureAssessment(
            status=status,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            records=tuple(records),
        )

    def completeness(self) -> tuple[bool, list[str], list[dict[str, Any]]]:
        """Compatibility view for callers that only distinguish perfect captures."""
        assessment = self.assessment()
        reasons = [*assessment.blockers, *assessment.warnings]
        return (
            assessment.status == "complete",
            reasons,
            list(assessment.records),
        )

    def diagnostics(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        warnings: Sequence[str] = (),
    ) -> dict[str, Any]:
        roots = sum(row["comment_type"] == "root" for row in records)
        replies = len(records) - roots
        return {
            "captured_records": len(records),
            "observed_records": len(self._comments),
            "rejected_records": max(0, len(self._comments) - len(records)),
            "root_comments": roots,
            "reply_comments": replies,
            "root_pages": self._root_pages,
            "reply_pages": self._reply_pages,
            "root_pagination_closed": self._root_terminal_seen,
            "reported_total": self._reported_total,
            "declared_reply_roots": sum(
                count > 0 for count in self._root_expected_replies.values()
            ),
            "warnings": list(warnings),
        }


def _validate_browser_profile_dir(profile: Path) -> None:
    require_safe_runtime_path(
        profile,
        project_root=REPOSITORY_ROOT,
        source_subdirectory=Path("var") / "sessions",
        label="browser_profile_dir",
    )


def _page_blocker(page: Any) -> str | None:
    current_url = str(getattr(page, "url", "")).lower()
    if any(token in current_url for token in ("captcha", "verify", "passport")):
        return "Douyin redirected the browser to login or verification"
    try:
        scopes = page.locator(BLOCKER_SCOPE_SELECTOR)
        scope_count = min(scopes.count(), 20)
    except Exception:
        return None
    for index in range(scope_count):
        scope = scopes.nth(index)
        try:
            if not scope.is_visible():
                continue
            text = scope.inner_text(timeout=1_000)
        except Exception:
            continue
        for marker in CHALLENGE_TEXTS:
            if marker in text:
                return f"Douyin verification is required ({marker})"
        for marker in LOGIN_TEXTS:
            if marker in text:
                return "The persistent browser profile is not logged in to Douyin"
    return None


def _drive_comment_view(page: Any) -> None:
    """Best-effort expansion; the response accumulator remains the authority."""
    try:
        matches = page.get_by_text(EXPAND_TEXT_RE)
        for index in range(min(matches.count(), 24)):
            candidate = matches.nth(index)
            if candidate.is_visible():
                try:
                    candidate.click(timeout=700)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        page.mouse.wheel(0, 1_200)
        page.evaluate(
            """
            () => {
              window.scrollBy(0, 500);
              for (const element of document.querySelectorAll('*')) {
                const style = getComputedStyle(element);
                if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                    element.scrollHeight > element.clientHeight + 100) {
                  element.scrollTop += Math.max(500, element.clientHeight * 0.8);
                }
              }
            }
            """
        )
    except Exception:
        pass


def _launch_persistent_context(playwright: Any, profile: Path) -> Any:
    """Use bundled Chromium when available, then installed stable browsers."""
    options = {
        "headless": False,
        "locale": "zh-CN",
        "viewport": {"width": 1440, "height": 960},
    }
    failures: list[str] = []
    for channel in (None, "chrome", "msedge"):
        try:
            if channel is None:
                return playwright.chromium.launch_persistent_context(
                    str(profile), **options
                )
            return playwright.chromium.launch_persistent_context(
                str(profile), channel=channel, **options
            )
        except Exception as exc:
            failures.append(f"{channel or 'playwright'}: {type(exc).__name__}")
    raise RuntimeError(
        "No compatible Chromium browser could be launched ("
        + ", ".join(failures)
        + "); run `python -m playwright install chromium`"
    )


def _is_relevant_comment_response(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold()
    if not (hostname == "douyin.com" or hostname.endswith(".douyin.com")):
        return False
    return bool(
        COMMENT_ENDPOINT_RE.search(parsed.path)
        or "aweme/detail" in parsed.path
        or "aweme/post" in parsed.path
    )


def collect_video(
    *,
    video_id: str,
    video_url: str | None = None,
    video_title: str = "",
    batches_dir: Path | None = None,
    browser_profile_dir: Path | None = None,
    capture_seconds: int = 120,
    trigger: str = "auto",
) -> CollectionResult:
    """Open a headed persistent browser and capture one video conservatively."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("video_id must contain 8-32 digits")
    if not 10 <= capture_seconds <= 900:
        raise ValueError("capture_seconds must be between 10 and 900")
    if trigger not in {"auto", "manual"}:
        raise ValueError("trigger must be auto or manual")
    url = video_url or f"https://www.douyin.com/video/{video_id}"
    parsed_url = urlparse(url)
    if parsed_url.scheme != "https" or not (
        parsed_url.hostname == "douyin.com"
        or (parsed_url.hostname or "").endswith(".douyin.com")
    ):
        raise ValueError("video_url must be an HTTPS URL on douyin.com")

    batches = (
        batches_dir
        or default_browser_profile_dir().parent / "comments" / "batches"
    ).resolve()
    profile = (browser_profile_dir or default_browser_profile_dir()).resolve()
    _validate_browser_profile_dir(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        profile.chmod(0o700)
    except OSError:
        pass
    batches.mkdir(parents=True, exist_ok=True)
    collected_at = utc_now()
    day = collected_at[:10]
    batch_name = f"{day}-{video_id}-{trigger}-{uuid.uuid4().hex[:10]}"
    accumulator = ResponseAccumulator(
        video_id=video_id,
        video_url=url,
        video_title=video_title,
        collected_at=collected_at,
        batch_name=batch_name,
    )

    browser_failure: str | None = None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        browser_failure = (
            "Playwright is not installed; install the Python package and Chromium"
        )
    else:
        try:
            with sync_playwright() as playwright:
                context = _launch_persistent_context(playwright, profile)
                try:
                    page = context.pages[0] if context.pages else context.new_page()

                    def handle_response(response: Any) -> None:
                        if not _is_relevant_comment_response(str(response.url)):
                            return
                        try:
                            accumulator.consume(response.url, response.json())
                        except Exception as exc:
                            accumulator.add_error(
                                "A recognized response could not be decoded: "
                                f"{type(exc).__name__}"
                            )

                    page.on("response", handle_response)
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    try:
                        accumulator.set_page_title(page.title())
                    except Exception:
                        pass
                    deadline = time.monotonic() + capture_seconds
                    complete_streak = 0
                    active_blocker: str | None = None
                    while time.monotonic() < deadline:
                        blocker = _page_blocker(page)
                        if blocker:
                            # Keep the headed window open so the operator can log
                            # in or solve a verification challenge during this run.
                            active_blocker = blocker
                            page.wait_for_timeout(1_000)
                            continue
                        active_blocker = None
                        _drive_comment_view(page)
                        page.wait_for_timeout(1_000)
                        assessment = accumulator.assessment()
                        complete_streak = (
                            complete_streak + 1 if assessment.is_finished else 0
                        )
                        if complete_streak >= 3:
                            break
                    if active_blocker:
                        browser_failure = active_blocker
                finally:
                    try:
                        session = context.new_cdp_session(page)
                        session.send("Network.clearBrowserCache")
                        session.detach()
                    except Exception:
                        pass
                    context.close()
        except Exception as exc:
            browser_failure = f"Browser capture failed: {type(exc).__name__}: {exc}"

    assessment = accumulator.assessment()
    status = assessment.status
    blockers = list(assessment.blockers)
    warnings = list(assessment.warnings)
    records = list(assessment.records)
    if browser_failure:
        blockers.insert(0, browser_failure)
        status = "blocked"
    batch_path: Path | None = None
    if records:
        batch_path = batches / f"{batch_name}.jsonl"
        write_jsonl_atomic(batch_path, records)

    diagnostics = accumulator.diagnostics(records, warnings=warnings)
    diagnostics["batch_name"] = batch_name
    if status == "complete":
        message = f"Capture complete: {len(records)} anonymous comments and replies"
    elif status == "partial":
        message = (
            f"Capture partial: {len(records)} accessible anonymous comments and replies; "
            + "; ".join(warnings)
        )
    else:
        message = "; ".join(dict.fromkeys([*blockers, *warnings])) or (
            "Completeness could not be established"
        )
    return CollectionResult(
        status=status,
        message=message,
        video_id=video_id,
        records=tuple(records),
        batch_path=batch_path,
        diagnostics=diagnostics,
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def update_collection_metadata(
    result: CollectionResult,
    *,
    targets_path: Path,
    progress_path: Path,
    canonical_comments_path: Path,
) -> None:
    """Record complete/partial/blocked outcome without downgrading prior complete work."""
    targets = _load_json(targets_path)
    progress = _load_json(progress_path)
    target = next(
        (
            item
            for item in targets.get("videos", [])
            if isinstance(item, dict) and item.get("video_id") == result.video_id
        ),
        None,
    )
    if target is None:
        raise ValueError(f"video {result.video_id} is not in the target manifest")
    if target.get("status") == "complete" and result.status != "complete":
        return

    now = utc_now()
    target["status"] = result.status
    targets["generated_at"] = now
    targets["completed_video_count"] = sum(
        isinstance(item, dict) and item.get("status") == "complete"
        for item in targets.get("videos", [])
    )

    canonical = (
        load_records([canonical_comments_path]) if canonical_comments_path.exists() else []
    )
    stored_for_video = sum(row["video_id"] == result.video_id for row in canonical)
    progress_videos = progress.setdefault("videos", {})
    batch_name = result.diagnostics.get("batch_name", "")
    progress_videos[result.video_id] = {
        "title": target.get("title", ""),
        "url": target.get("video_url", ""),
        "status": result.status,
        "visible_comment_count": len(result.records),
        "stored_record_count": stored_for_video,
        "last_batch": batch_name if result.batch_path else "",
        "last_collected_at": (
            result.records[0]["collected_at"] if result.records else None
        ),
        "notes": result.message,
    }
    target_count = len(targets.get("videos", []))
    completed_count = sum(
        isinstance(item, dict) and item.get("status") == "complete"
        for item in progress_videos.values()
    )
    progress["target_video_count"] = target_count
    progress["completed_video_count"] = completed_count
    progress["coverage_ratio"] = round(
        completed_count / target_count if target_count else 0.0, 3
    )
    progress["stored_record_count"] = sum(
        _safe_count(item.get("stored_record_count", 0))
        for item in progress_videos.values()
        if isinstance(item, Mapping)
    )
    progress["updated_at"] = now

    write_collection_state(targets_path, targets, progress_path, progress)
