from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Any, Mapping

import pytest

import admin_app.collector as collector_module
import admin_app.jobs as jobs_module
import admin_app.runtime_paths as runtime_paths
from admin_app.collector import (
    CollectionResult,
    EXPAND_TEXT_RE,
    ResponseAccumulator,
    _drive_comment_view,
    _is_relevant_comment_response,
    _launch_persistent_context,
    _validate_browser_profile_dir,
)
from admin_app.config import Settings
from admin_app.archive_lock import archive_lock
from admin_app.jobs import JobExecution, JobWorker


VIDEO_ID = "7654321098765432109"
ROOT_ID = "1111111111111111111"
REPLY_ID = "2222222222222222222"


def test_comment_browser_listener_only_accepts_douyin_responses() -> None:
    assert _is_relevant_comment_response(
        "https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=123"
    )
    assert not _is_relevant_comment_response(
        "https://example.com/aweme/v1/web/comment/list/?aweme_id=123"
    )
    assert not _is_relevant_comment_response(
        "https://douyin.com.evil.example/aweme/v1/web/comment/list/"
    )


def _accumulator(
    *, declared_replies: int = 1, include_reply: bool = True
) -> ResponseAccumulator:
    accumulator = ResponseAccumulator(
        video_id=VIDEO_ID,
        video_url=f"https://www.douyin.com/video/{VIDEO_ID}",
        video_title="双离合低速工况",
        collected_at="2026-09-06T04:00:00Z",
        batch_name="2026-09-06-test",
    )
    accumulator.consume(
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}",
        {
            "status_code": 0,
            "aweme_detail": {
                "aweme_id": VIDEO_ID,
                "desc": "双离合低速工况",
                "author": {
                    "uid": "creator-uid",
                    "sec_uid": "creator-sec-uid",
                    "nickname": "不应持久化的作者昵称",
                },
            },
        },
    )
    accumulator.consume(
        f"https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id={VIDEO_ID}",
        {
            "status_code": 0,
            "has_more": 0,
            "total": 1,
            "comments": [
                {
                    "cid": ROOT_ID,
                    "aweme_id": VIDEO_ID,
                    "text": "低速跟车怎么开？",
                    "digg_count": 7,
                    "reply_comment_total": declared_replies,
                    "create_time": 1_788_667_200,
                    "user": {
                        "uid": "viewer-uid",
                        "sec_uid": "viewer-sec-uid",
                        "nickname": "不应持久化的观众昵称",
                        "avatar_thumb": {"url_list": ["https://example.invalid/a.jpg"]},
                    },
                }
            ],
        },
    )
    if include_reply:
        accumulator.consume(
            "https://www.douyin.com/aweme/v1/web/comment/list/reply/"
            f"?aweme_id={VIDEO_ID}&comment_id={ROOT_ID}",
            {
                "status_code": 0,
                "has_more": False,
                "comments": [
                    {
                        "cid": REPLY_ID,
                        "aweme_id": VIDEO_ID,
                        "root_comment_id": ROOT_ID,
                        "text": "保持车距，避免长时间蠕行。",
                        "digg_count": 3,
                        "create_time": 1_788_667_260,
                        "user": {
                            "uid": "creator-uid",
                            "sec_uid": "creator-sec-uid",
                            "nickname": "不应持久化的作者昵称",
                            "signature": "不应持久化的签名",
                        },
                    },
                ],
            },
        )
    return accumulator


def test_response_accumulator_materializes_complete_root_and_reply() -> None:
    complete, reasons, records = _accumulator().completeness()

    assert complete is True
    assert reasons == []
    assert [record["comment_id"] for record in records] == [ROOT_ID, REPLY_ID]
    root, reply = records
    assert root["comment_type"] == "root"
    assert root["author_role"] == "viewer"
    assert root["parent_comment_id"] is None
    assert root["root_comment_id"] is None
    assert reply["comment_type"] == "reply"
    assert reply["author_role"] == "creator"
    assert reply["parent_comment_id"] == ROOT_ID
    assert reply["root_comment_id"] == ROOT_ID


def test_creator_identity_tokens_do_not_collide_across_id_namespaces() -> None:
    accumulator = ResponseAccumulator(
        video_id=VIDEO_ID,
        video_url=f"https://www.douyin.com/video/{VIDEO_ID}",
        collected_at="2026-09-06T04:00:00Z",
    )
    accumulator.consume(
        f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={VIDEO_ID}",
        {
            "status_code": 0,
            "aweme_detail": {
                "aweme_id": VIDEO_ID,
                "author": {"unique_id": "12345678"},
            },
        },
    )
    accumulator.consume(
        f"https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id={VIDEO_ID}",
        {
            "status_code": 0,
            "has_more": 0,
            "total": 1,
            "comments": [
                {
                    "cid": ROOT_ID,
                    "aweme_id": VIDEO_ID,
                    "text": "同名空间碰撞测试",
                    "reply_comment_total": 0,
                    "user": {"uid": "12345678"},
                }
            ],
        },
    )

    assessment = accumulator.assessment()

    assert assessment.status == "complete"
    assert assessment.records[0]["author_role"] == "viewer"


def test_terminal_zero_comment_response_is_complete_without_author_metadata() -> None:
    accumulator = ResponseAccumulator(
        video_id=VIDEO_ID,
        video_url=f"https://www.douyin.com/video/{VIDEO_ID}",
        collected_at="2026-09-06T04:00:00Z",
    )
    accumulator.consume(
        f"https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id={VIDEO_ID}",
        {"status_code": 0, "has_more": 0, "total": 0, "comments": []},
    )

    assessment = accumulator.assessment()

    assert assessment.status == "complete"
    assert assessment.blockers == ()
    assert assessment.records == ()


def test_response_accumulator_marks_terminal_reply_gap_partial() -> None:
    accumulator = _accumulator(declared_replies=2)
    assessment = accumulator.assessment()

    assert assessment.status == "partial"
    assert assessment.blockers == ()
    assert len(assessment.records) == 2
    assert any(
        f"Root {ROOT_ID} declares 2 replies but 1 were captured" in reason
        for reason in assessment.warnings
    )


def test_response_accumulator_blocks_when_reply_pagination_is_open() -> None:
    assessment = _accumulator(
        declared_replies=2, include_reply=False
    ).assessment()

    assert assessment.status == "blocked"
    assert assessment.warnings == ()
    assert any(
        f"Root {ROOT_ID} declares 2 replies but 0 were captured" in reason
        and "pagination not closed" in reason
        for reason in assessment.blockers
    )


def test_response_accumulator_isolates_reply_with_unavailable_parent() -> None:
    accumulator = _accumulator(declared_replies=1, include_reply=False)
    orphan_id = "3333333333333333333"
    accumulator.consume(
        "https://www.douyin.com/aweme/v1/web/comment/list/reply/"
        f"?aweme_id={VIDEO_ID}&comment_id={ROOT_ID}",
        {
            "status_code": 0,
            "has_more": False,
            "comments": [
                {
                    "cid": orphan_id,
                    "aweme_id": VIDEO_ID,
                    "root_comment_id": ROOT_ID,
                    "reply_to_reply_id": "4444444444444444444",
                    "text": "父评论已不可见",
                    "digg_count": 0,
                    "create_time": 1_788_667_300,
                    "user": {"uid": "viewer-orphan"},
                }
            ],
        },
    )

    assessment = accumulator.assessment()
    diagnostics = accumulator.diagnostics(
        assessment.records, warnings=assessment.warnings
    )

    assert assessment.status == "partial"
    assert [row["comment_id"] for row in assessment.records] == [ROOT_ID]
    assert any(orphan_id in warning for warning in assessment.warnings)
    assert diagnostics["observed_records"] == 2
    assert diagnostics["captured_records"] == 1
    assert diagnostics["rejected_records"] == 1
    assert diagnostics["warnings"] == list(assessment.warnings)


def test_response_accumulator_closes_multi_page_replies() -> None:
    accumulator = _accumulator(declared_replies=5, include_reply=False)
    for page, (cursor, has_more, reply_numbers) in enumerate(
        (("0", True, range(1, 4)), ("3", False, range(4, 6)))
    ):
        accumulator.consume(
            "https://www.douyin.com/aweme/v1/web/comment/list/reply/"
            f"?aweme_id={VIDEO_ID}&comment_id={ROOT_ID}&cursor={cursor}",
            {
                "status_code": 0,
                "has_more": has_more,
                "comments": [
                    {
                        "cid": str(3_000_000_000_000_000_000 + number),
                        "aweme_id": VIDEO_ID,
                        "root_comment_id": ROOT_ID,
                        "text": f"reply {number}",
                        "digg_count": 0,
                        "create_time": 1_788_667_260 + page * 10 + number,
                        "user": {"uid": f"viewer-{number}"},
                    }
                    for number in reply_numbers
                ],
            },
        )

    complete, reasons, records = accumulator.completeness()

    assert complete is True
    assert reasons == []
    assert len(records) == 6
    assert accumulator.diagnostics(records)["reply_pages"] == 2


def test_response_accumulator_never_materializes_personal_identity_fields() -> None:
    complete, _, records = _accumulator().completeness()

    assert complete is True
    forbidden_fields = {
        "author",
        "user",
        "uid",
        "user_id",
        "sec_uid",
        "short_id",
        "unique_id",
        "nickname",
        "signature",
        "avatar_thumb",
    }
    for record in records:
        assert forbidden_fields.isdisjoint(record)
        serialized_values = repr(record)
        assert "creator-sec-uid" not in serialized_values
        assert "viewer-sec-uid" not in serialized_values
        assert "不应持久化" not in serialized_values


class FakeRepository:
    def __init__(self, job: Mapping[str, Any]) -> None:
        self.job = dict(job)
        self.claimed_by: list[str] = []
        self.transitions: list[tuple[str, int, dict[str, Any]]] = []

    def claim_next_job(self, worker_id: str, *, now=None) -> dict[str, Any] | None:
        self.claimed_by.append(worker_id)
        job, self.job = self.job, {}
        return job or None

    def heartbeat_job(self, job_id: int, worker_id: str) -> bool:
        return True

    def mark_job_succeeded(
        self,
        job_id: int,
        result: Mapping[str, Any] | None = None,
        message: str | None = None,
        worker_id: str | None = None,
    ) -> bool:
        self.transitions.append(
            (
                "succeeded",
                job_id,
                {"result": result, "message": message, "worker_id": worker_id},
            )
        )
        return True

    def mark_job_blocked(
        self,
        job_id: int,
        message: str,
        result: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> bool:
        self.transitions.append(
            (
                "blocked",
                job_id,
                {"result": result, "message": message, "worker_id": worker_id},
            )
        )
        return True

    def mark_job_failed(
        self,
        job_id: int,
        message: str,
        worker_id: str | None = None,
    ) -> bool:
        self.transitions.append(
            ("failed", job_id, {"message": message, "worker_id": worker_id})
        )
        return True


@pytest.mark.parametrize(
    ("execution", "expected_transition"),
    [
        (
            JobExecution("succeeded", "采集完成", {"captured": 12}),
            "succeeded",
        ),
        (
            JobExecution("blocked", "需要登录", {"captured": 0}),
            "blocked",
        ),
    ],
)
def test_job_worker_persists_terminal_execution_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: JobExecution,
    expected_transition: str,
) -> None:
    repository = FakeRepository({"id": 41, "job_type": "collect_video"})
    settings = Settings(
        repository_root=tmp_path,
        db_password="database-secret",
        browser_profile_dir=tmp_path.parent / "browser-profile",
    )
    worker = JobWorker(repository, settings, worker_id="worker-test")
    monkeypatch.setattr(worker, "execute", lambda _job: execution)

    assert worker.run_once() is True
    assert repository.claimed_by == ["worker-test"]
    assert repository.transitions == [
        (
            expected_transition,
            41,
            {
                "result": execution.result,
                "message": execution.message,
                "worker_id": "worker-test",
            },
        )
    ]


def test_partial_capture_is_a_successful_job_with_partial_target_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_dir = tmp_path / "workspace"
    comments_dir = workspace_dir / "comments"
    comments_dir.mkdir(parents=True)
    (comments_dir / "collection-targets.json").write_text(
        json.dumps(
            {
                "videos": [
                    {
                        "video_id": VIDEO_ID,
                        "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                        "title": "测试作品",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        repository_root=tmp_path,
        workspace_dir=workspace_dir,
        db_password="database-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    capture = CollectionResult(
        status="partial",
        message="Capture partial: one unavailable reply",
        video_id=VIDEO_ID,
        records=(),
        batch_path=None,
        diagnostics={"warnings": ["one unavailable reply"]},
    )
    worker = JobWorker(
        FakeRepository({}),
        settings,
        worker_id="worker-test",
        collector=lambda **_: capture,
    )
    recorded_statuses: list[str] = []

    def record_metadata(result: CollectionResult, **_: Any) -> None:
        recorded_statuses.append(result.status)

    monkeypatch.setattr(jobs_module, "update_collection_metadata", record_metadata)
    monkeypatch.setattr(worker, "_sync_mysql", lambda: ({}, {}))

    execution = worker._collect_video({"video_id": VIDEO_ID, "payload": {}})

    assert execution.status == "succeeded"
    assert execution.message == capture.message
    assert recorded_statuses == ["partial"]


def test_job_worker_marks_exception_failed_and_redacts_database_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeRepository({"id": 42, "job_type": "sync_mysql"})
    settings = Settings(
        repository_root=tmp_path,
        db_password="database-secret",
        browser_profile_dir=tmp_path.parent / "browser-profile",
    )
    worker = JobWorker(repository, settings, worker_id="worker-test")

    def fail(_job: Mapping[str, Any]) -> JobExecution:
        raise RuntimeError("connection failed with database-secret")

    monkeypatch.setattr(worker, "execute", fail)

    assert worker.run_once() is True
    assert repository.transitions == [
        (
            "failed",
            42,
            {
                "message": "RuntimeError: connection failed with [redacted]",
                "worker_id": "worker-test",
            },
        )
    ]


def test_worker_mysql_sync_rolls_back_all_data_when_creator_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TransactionConnection:
        def __init__(self) -> None:
            self.events: list[str] = []

        def begin(self) -> None:
            self.events.append("begin")

        def commit(self) -> None:
            self.events.append("commit")

        def rollback(self) -> None:
            self.events.append("rollback")

        def close(self) -> None:
            self.events.append("close")

    settings = Settings(
        repository_root=tmp_path,
        db_password="database-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    worker = JobWorker(FakeRepository({}), settings, worker_id="worker-test")
    connection = TransactionConnection()
    monkeypatch.setattr(
        jobs_module.import_comments_mysql, "load_import_payload", lambda *_: object()
    )
    monkeypatch.setattr(
        jobs_module.import_comments_mysql,
        "create_database_if_missing",
        lambda **_: None,
    )
    monkeypatch.setattr(
        jobs_module.import_comments_mysql, "connect_mysql", lambda **_: connection
    )

    def import_comments(*args, **kwargs):
        assert args[0] is connection
        assert kwargs["manage_transaction"] is False
        assert kwargs["verify"] is False
        return {"canonical_comment_count": 1}, {}

    def fail_creator_sync(*args, **kwargs):
        assert args == (connection, settings)
        assert kwargs == {"manage_transaction": False}
        raise RuntimeError("creator source failed")

    monkeypatch.setattr(
        jobs_module.import_comments_mysql, "import_payload", import_comments
    )
    monkeypatch.setattr(jobs_module, "sync_creator_sources", fail_creator_sync)
    monkeypatch.setattr(
        jobs_module.import_comments_mysql,
        "verify_database",
        lambda *_: pytest.fail("verification must not run after creator sync failed"),
    )

    with pytest.raises(RuntimeError, match="creator source failed"):
        worker._sync_mysql()

    assert connection.events == ["begin", "rollback", "close"]


def test_analysis_holds_archive_lock_while_reading_and_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        db_password="database-secret",
        browser_profile_dir=tmp_path.parent / "browser-profile",
    )
    worker = JobWorker(FakeRepository({}), settings, worker_id="worker-test")
    state = {"locked": False}

    @contextmanager
    def tracking_lock(path: Path):
        assert path == settings.comments_dir / ".archive.lock"
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    def analyze(_argv: list[str]) -> int:
        assert state["locked"] is True
        return 0

    def load_records(_paths: list[Path]) -> list[dict[str, str]]:
        assert state["locked"] is True
        return [{"comment_id": "comment-1"}]

    monkeypatch.setattr(jobs_module, "archive_lock", tracking_lock)
    monkeypatch.setattr(jobs_module.analyze_comments, "main", analyze)
    monkeypatch.setattr(jobs_module.analyze_comments, "load_records", load_records)

    result = worker._analyze({"payload": {}})

    assert result.status == "succeeded"
    assert result.result["comment_count"] == 1
    assert state["locked"] is False


class FakeChromium:
    def __init__(self) -> None:
        self.channels: list[str | None] = []
        self.context = object()

    def launch_persistent_context(self, profile: str, **options: Any) -> object:
        channel = options.get("channel")
        self.channels.append(channel)
        assert profile.endswith("browser-profile")
        assert options["headless"] is False
        if channel in {None, "chrome"}:
            raise RuntimeError("browser candidate unavailable")
        return self.context


def test_persistent_browser_launch_falls_back_in_candidate_order(
    tmp_path: Path,
) -> None:
    chromium = FakeChromium()
    playwright = type("FakePlaywright", (), {"chromium": chromium})()

    context = _launch_persistent_context(playwright, tmp_path / "browser-profile")

    assert context is chromium.context
    assert chromium.channels == [None, "chrome", "msedge"]


def test_comment_driver_expands_and_scrolls() -> None:
    clicked: list[int] = []
    wheels: list[tuple[int, int]] = []
    evaluations: list[str] = []

    class Candidate:
        def __init__(self, index: int) -> None:
            self.index = index

        def is_visible(self) -> bool:
            return True

        def click(self, *, timeout: int) -> None:
            assert timeout == 700
            clicked.append(self.index)

    class Matches:
        def count(self) -> int:
            return 30

        def nth(self, index: int) -> Candidate:
            return Candidate(index)

    class Mouse:
        def wheel(self, x: int, y: int) -> None:
            wheels.append((x, y))

    class Page:
        mouse = Mouse()

        def get_by_text(self, pattern) -> Matches:
            assert pattern.fullmatch("展开更多")
            return Matches()

        def evaluate(self, script: str) -> None:
            evaluations.append(script)

    _drive_comment_view(Page())

    assert clicked == list(range(24))
    assert wheels == [(0, 1_200)]
    assert len(evaluations) == 1
    assert "scrollTop" in evaluations[0]


@pytest.mark.parametrize(
    "label",
    ["展开5条回复", "展开 5 条回复", "展开更多", "展开更多回复", "查看全部回复", "更多回复"],
)
def test_reply_expansion_text_matches_known_douyin_labels(label: str) -> None:
    assert EXPAND_TEXT_RE.fullmatch(label)


@pytest.mark.parametrize("label", ["展开更多内容", "展开全文", "收起回复"])
def test_reply_expansion_text_ignores_unrelated_controls(label: str) -> None:
    assert EXPAND_TEXT_RE.fullmatch(label) is None


def test_browser_profile_inside_repository_is_limited_to_var_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collector_module, "REPOSITORY_ROOT", tmp_path)

    _validate_browser_profile_dir(tmp_path / "var" / "sessions" / "creator-a")
    _validate_browser_profile_dir(tmp_path.parent / "external-sessions" / "creator-a")

    with pytest.raises(ValueError, match="must not be stored inside"):
        _validate_browser_profile_dir(tmp_path / "browser-profile" / "creator-a")


def test_default_browser_profile_uses_user_data_for_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collector_module, "REPOSITORY_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(runtime_paths, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))

    assert collector_module.default_browser_profile_dir() == (
        tmp_path / "user-data" / "csi-openbase" / "sessions" / "default"
    ).resolve()


def test_archive_lock_is_reentrant_and_serializes_threads(tmp_path: Path) -> None:
    path = tmp_path / ".archive.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with archive_lock(path):
            with archive_lock(path):
                first_entered.set()
                assert release_first.wait(timeout=2)

    def second() -> None:
        assert first_entered.wait(timeout=2)
        with archive_lock(path):
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered.is_set()
