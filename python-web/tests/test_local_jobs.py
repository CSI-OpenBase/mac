from __future__ import annotations

import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from admin_app.collector import CollectionResult
from admin_app.douyin_exports import ExportFileResult, ExportRunResult
from admin_app.local_browser import CreatorIdentity
from admin_app.local_config import (
    COMMENT_EXPORT_DIRECTORY_KEY,
    LOCAL_PREFERENCES_META_KEY,
    LocalSettings,
)
from admin_app.local_jobs import LocalJobRunner
from admin_app.local_store import LocalStore
from admin_app.video_archive import archive_profile_videos


VIDEO_ID = "7680023068660346011"


def make_settings(tmp_path: Path) -> LocalSettings:
    settings = LocalSettings(
        data_home=tmp_path,
        session_home=tmp_path / ".sessions",
        authorization_timeout_seconds=30,
        browser_capture_seconds=10,
    )
    settings.ensure_directories()
    return settings


def wait_for_job(store: LocalStore, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        assert job is not None
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("local job did not finish")


def identity(**_: Any) -> CreatorIdentity:
    return CreatorIdentity(
        handle="creator-handle",
        display_name="测试创作者",
        authorized_at="2026-09-07T00:00:00Z",
    )


def test_authorization_job_runs_only_after_explicit_submit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    runner = LocalJobRunner(store, settings, authorize=identity)
    try:
        assert store.list_jobs() == []
        job = runner.submit("authorize")
        finished = wait_for_job(store, job["id"])
        assert finished["status"] == "succeeded"
        assert store.get_meta("creator_identity")["handle"] == "creator-handle"
    finally:
        runner.close()


def test_close_persists_interrupted_before_forceful_process_cleanup(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    started = threading.Event()
    release = threading.Event()

    def slow_authorize(**_: Any) -> CreatorIdentity:
        started.set()
        release.wait(timeout=10)
        return identity()

    runner = LocalJobRunner(store, settings, authorize=slow_authorize)
    job = runner.submit("authorize")
    assert started.wait(timeout=1)
    queued = runner.submit("authorize")
    assert runner.close() is False

    interrupted = store.get_job(job["id"])
    assert interrupted is not None
    assert interrupted["status"] == "interrupted"
    assert interrupted["message"] == "应用关闭，任务已中断"
    assert store.get_job(queued["id"])["status"] == "interrupted"
    assert store.get_meta("creator_identity", {}) == {}

    release.set()
    assert runner._thread is not None
    runner._thread.join(timeout=1)
    assert not runner._thread.is_alive()
    assert store.get_job(job["id"])["status"] == "interrupted"
    assert store.get_meta("creator_identity", {}) == {}


def test_submit_cannot_insert_a_job_after_shutdown_interrupts_queue(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    runner = LocalJobRunner(store, settings, authorize=identity)
    entered_create = threading.Event()
    allow_create = threading.Event()
    original_create = store.create_job
    submitted: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def delayed_create(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered_create.set()
        assert allow_create.wait(timeout=2)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_job", delayed_create)

    def submit() -> None:
        try:
            submitted.append(runner.submit("authorize"))
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    submit_thread = threading.Thread(target=submit)
    close_thread = threading.Thread(target=runner.close)
    submit_thread.start()
    assert entered_create.wait(timeout=1)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    allow_create.set()
    submit_thread.join(timeout=2)
    close_thread.join(timeout=3)

    assert not errors
    assert len(submitted) == 1
    assert not submit_thread.is_alive()
    assert not close_thread.is_alive()
    assert store.active_job_count() == 0
    assert store.get_job(submitted[0]["id"])["status"] in {
        "succeeded",
        "interrupted",
    }

def test_video_sync_indexes_idempotent_file_archive(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.set_meta("creator_identity", identity().as_dict())

    calls = 0

    def fake_sync(**kwargs: Any):
        nonlocal calls
        calls += 1
        assert kwargs["profile_url"] == (
            "https://creator.douyin.com/creator-micro/content/manage"
        )
        assert kwargs["expected_handle"] == "creator-handle"
        record: dict[str, Any] = {
            "video_id": VIDEO_ID,
            "title": "测试视频",
            "url": f"https://www.douyin.com/video/{VIDEO_ID}",
            "published_at": "2026-09-01T00:30:00Z",
            "view_count": 100 + calls,
            "sources": ["response"],
            "platform_groups": [
                {"id": "7348687990509553679", "name": "测试栏目"}
            ],
        }
        if calls == 1:
            record["comment_count"] = 34
        return archive_profile_videos(
            profile_url=kwargs["profile_url"],
            works_dir=kwargs["works_dir"],
            records=[record],
            observed_at=f"2026-09-0{calls + 6}T00:00:00Z",
        )

    runner = LocalJobRunner(
        store, settings, authorize=identity, video_sync=fake_sync
    )
    try:
        first = runner.submit(
            "sync_videos", profile_url="https://www.douyin.com/user/someone-else"
        )
        assert wait_for_job(store, first["id"])["status"] == "succeeded"
        second = runner.submit(
            "sync_videos", profile_url="https://www.douyin.com/user/self"
        )
        assert wait_for_job(store, second["id"])["status"] == "succeeded"
    finally:
        runner.close()

    videos = store.list_videos()
    assert len(videos) == 1
    assert videos[0]["first_seen_at"] == "2026-09-07T00:00:00Z"
    assert videos[0]["last_seen_at"] == "2026-09-08T00:00:00Z"
    assert videos[0]["record"]["published_at"] == "2026-09-01T00:30:00Z"
    assert videos[0]["visible_comment_count"] == 34
    assert videos[0]["comment_count"] == 0
    assert [(group["name"], group["source"]) for group in videos[0]["groups"]] == [
        ("测试栏目", "platform")
    ]


def test_comment_export_is_timestamped_and_records_user_trigger(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    external_root = tmp_path / "comment-exports"
    store.set_meta(
        LOCAL_PREFERENCES_META_KEY,
        {COMMENT_EXPORT_DIRECTORY_KEY: str(external_root)},
    )
    archive_profile_videos(
        profile_url="https://www.douyin.com/user/self",
        works_dir=settings.works_dir,
        records=[
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "url": f"https://www.douyin.com/video/{VIDEO_ID}",
            }
        ],
        observed_at="2026-09-07T00:00:00Z",
    )
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
            }
        ]
    )

    def fake_comments(**kwargs: Any) -> CollectionResult:
        assert kwargs["trigger"] == "manual"
        directory = Path(kwargs["batches_dir"])
        checkpoint = Path(kwargs["checkpoint_path"])
        assert checkpoint == directory / "comments-checkpoint.jsonl"
        running_manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        assert running_manifest["status"] == "running"
        assert running_manifest["usable_as_baseline"] is False
        assert running_manifest["snapshot_file"] == checkpoint.name
        path = directory / "comments.jsonl"
        record = {
            "video_id": VIDEO_ID,
            "comment_id": "comment-1",
            "collected_at": "2026-09-07T01:00:00Z",
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return CollectionResult(
            status="complete",
            message="complete",
            video_id=VIDEO_ID,
            records=(record,),
            batch_path=path,
            diagnostics={"anonymous": True},
        )

    runner = LocalJobRunner(store, settings, comment_collector=fake_comments)
    try:
        job = runner.submit("comments", video_id=VIDEO_ID)
        finished = wait_for_job(store, job["id"])
    finally:
        runner.close()

    assert finished["status"] == "succeeded"
    assert finished["payload"]["trigger"] == "user"
    assert finished["result"]["mode"] == "incremental"
    assert finished["result"]["new_count"] == 1
    assert finished["result"]["updated_count"] == 0
    assert finished["result"]["export_count"] == 1
    assert finished["result"]["file"].endswith("comments-incremental.jsonl")
    assert finished["result"]["snapshot_file"].endswith("comments.jsonl")
    assert "comments/20" in finished["result"]["directory"]
    external_file = Path(finished["result"]["export_file"])
    assert external_file.is_file()
    assert external_file.is_relative_to(external_root / VIDEO_ID)
    assert json.loads(external_file.read_text(encoding="utf-8"))["comment_id"] == (
        "comment-1"
    )
    assert "已导出到" in finished["message"]
    assert store.get_video(VIDEO_ID)["comment_count"] == 1


def test_incremental_comment_export_tracks_new_updates_and_retains_missing_history(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
            }
        ]
    )
    captures = [
        [
            {
                "platform": "douyin",
                "comment_id": "comment-1",
                "comment_type": "root",
                "parent_comment_id": None,
                "root_comment_id": None,
                "text": "保持不变",
                "like_count": 0,
                "collected_at": "2026-09-07T01:00:00Z",
                "collection_batch": "batch-1",
            },
            {
                "platform": "douyin",
                "comment_id": "comment-2",
                "comment_type": "root",
                "parent_comment_id": None,
                "root_comment_id": None,
                "text": "稍后更新",
                "like_count": 0,
                "collected_at": "2026-09-07T01:00:00Z",
                "collection_batch": "batch-1",
            },
        ],
        [
            {
                "platform": "douyin",
                "comment_id": "comment-1",
                "comment_type": "root",
                "parent_comment_id": None,
                "root_comment_id": None,
                "text": "保持不变",
                "like_count": 0,
                "collected_at": "2026-09-08T01:00:00Z",
                "collection_batch": "batch-2",
            },
            {
                "platform": "douyin",
                "comment_id": "comment-2",
                "comment_type": "root",
                "parent_comment_id": None,
                "root_comment_id": None,
                "text": "稍后更新",
                "like_count": 5,
                "collected_at": "2026-09-08T01:00:00Z",
                "collection_batch": "batch-2",
            },
            {
                "platform": "douyin",
                "comment_id": "comment-3",
                "comment_type": "reply",
                "parent_comment_id": "comment-1",
                "root_comment_id": "comment-1",
                "text": "新增评论",
                "like_count": 0,
                "collected_at": "2026-09-08T01:00:00Z",
                "collection_batch": "batch-2",
            },
        ],
        [
            {
                "platform": "douyin",
                "comment_id": "comment-1",
                "comment_type": "root",
                "parent_comment_id": None,
                "root_comment_id": None,
                "text": "保持不变",
                "like_count": 0,
                "collected_at": "2026-09-09T01:00:00Z",
                "collection_batch": "batch-3",
            },
            {
                "platform": "douyin",
                "comment_id": "comment-3",
                "comment_type": "reply",
                "parent_comment_id": "comment-1",
                "root_comment_id": "comment-1",
                "text": "新增评论",
                "like_count": 0,
                "collected_at": "2026-09-09T01:00:00Z",
                "collection_batch": "batch-3",
            },
        ],
    ]
    call_index = 0

    def fake_comments(**kwargs: Any) -> CollectionResult:
        nonlocal call_index
        records = captures[call_index]
        call_index += 1
        path = Path(kwargs["batches_dir"]) / f"batch-{call_index}.jsonl"
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return CollectionResult(
            status="complete",
            message="complete",
            video_id=VIDEO_ID,
            records=tuple(records),
            batch_path=path,
            diagnostics={"anonymous": True},
        )

    runner = LocalJobRunner(store, settings, comment_collector=fake_comments)
    try:
        first = wait_for_job(
            store,
            runner.submit("comments", video_id=VIDEO_ID, mode="incremental")["id"],
        )
        second = wait_for_job(
            store,
            runner.submit("comments", video_id=VIDEO_ID, mode="incremental")["id"],
        )
        third = wait_for_job(
            store,
            runner.submit("comments", video_id=VIDEO_ID, mode="full")["id"],
        )
    finally:
        runner.close()

    assert first["result"]["new_count"] == 2
    assert first["result"]["export_count"] == 2
    assert second["result"]["new_count"] == 1
    assert second["result"]["updated_count"] == 1
    assert second["result"]["unchanged_count"] == 1
    assert second["result"]["context_count"] == 1
    assert second["result"]["export_count"] == 3
    delta_path = settings.data_home / second["result"]["file"]
    delta = [
        json.loads(line)
        for line in delta_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["comment_id"] for record in delta] == [
        "comment-2",
        "comment-1",
        "comment-3",
    ]

    assert third["result"]["mode"] == "full"
    assert third["result"]["export_count"] == 2
    assert third["result"]["not_observed_count"] == 1
    assert "未从本地索引删除" in third["message"]
    canonical_path = (
        settings.works_dir
        / "videos"
        / "douyin"
        / VIDEO_ID
        / "comments"
        / "comments.jsonl"
    )
    canonical = [
        json.loads(line)
        for line in canonical_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["comment_id"] for record in canonical] == [
        "comment-1",
        "comment-2",
        "comment-3",
    ]
    assert canonical[1]["like_count"] == 5


def test_comment_count_job_records_delta_without_exporting_content(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
                "visible_comment_count": 34,
            }
        ]
    )
    calls: list[dict[str, Any]] = []

    def fake_count(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 39

    runner = LocalJobRunner(store, settings, comment_counter=fake_count)
    try:
        job = runner.submit("comment_count", video_id=VIDEO_ID)
        finished = wait_for_job(store, job["id"])
    finally:
        runner.close()

    assert finished["status"] == "succeeded"
    assert finished["payload"]["trigger"] == "user"
    assert finished["message"] == "评论数 39，较上次 +5"
    assert finished["result"]["previous_count"] == 34
    assert finished["result"]["current_count"] == 39
    assert finished["result"]["delta"] == 5
    assert finished["result"]["content_exported"] is False
    assert calls == [
        {
            "video_id": VIDEO_ID,
            "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
            "browser_profile_dir": settings.browser_profile_dir,
            "timeout_seconds": 10,
        }
    ]
    video = store.get_video(VIDEO_ID)
    assert video["visible_comment_count"] == 39
    assert video["comment_count"] == 0
    assert not (settings.works_dir / "videos" / "douyin" / VIDEO_ID / "comments").exists()


def test_blocked_comment_retry_does_not_replace_last_success(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
            }
        ]
    )
    store.record_comment_export(
        VIDEO_ID,
        count=27,
        exported_at="2026-09-07T01:00:00Z",
    )

    def blocked(**_: Any) -> CollectionResult:
        return CollectionResult(
            status="blocked",
            message="需要登录",
            video_id=VIDEO_ID,
            records=(),
            batch_path=None,
            diagnostics={"reason": "login"},
        )

    runner = LocalJobRunner(store, settings, comment_collector=blocked)
    try:
        job = runner.submit("comments", video_id=VIDEO_ID)
        assert wait_for_job(store, job["id"])["status"] == "blocked"
    finally:
        runner.close()

    video = store.get_video(VIDEO_ID)
    assert video["comment_count"] == 27
    assert video["last_comment_export_at"] == "2026-09-07T01:00:00Z"


def test_blocked_comment_capture_exports_progress_without_replacing_baseline(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    external_root = tmp_path / "comment-exports"
    store.set_meta(
        LOCAL_PREFERENCES_META_KEY,
        {COMMENT_EXPORT_DIRECTORY_KEY: str(external_root)},
    )
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
            }
        ]
    )
    comments_root = (
        settings.works_dir / "videos" / "douyin" / VIDEO_ID / "comments"
    )
    comments_root.mkdir(parents=True)
    baseline_record = {
        "platform": "douyin",
        "video_id": VIDEO_ID,
        "comment_id": "comment-old",
        "comment_type": "root",
        "parent_comment_id": None,
        "root_comment_id": None,
        "text": "已有完整基线",
        "collected_at": "2026-09-07T01:00:00Z",
    }
    canonical_path = comments_root / "comments.jsonl"
    canonical_path.write_text(
        json.dumps(baseline_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    store.record_comment_export(
        VIDEO_ID,
        count=1,
        exported_at="2026-09-07T01:00:00Z",
    )

    captured_record = {
        "platform": "douyin",
        "video_id": VIDEO_ID,
        "comment_id": "comment-new",
        "comment_type": "root",
        "parent_comment_id": None,
        "root_comment_id": None,
        "text": "失败前已经采集",
        "collected_at": "2026-09-08T01:00:00Z",
    }

    def blocked_with_progress(**kwargs: Any) -> CollectionResult:
        path = Path(kwargs["batches_dir"]) / "blocked-snapshot.jsonl"
        path.write_text(
            json.dumps(captured_record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return CollectionResult(
            status="blocked",
            message="回复分页未完成",
            video_id=VIDEO_ID,
            records=(captured_record,),
            batch_path=path,
            diagnostics={"root_pagination_closed": True},
        )

    runner = LocalJobRunner(
        store, settings, comment_collector=blocked_with_progress
    )
    try:
        job = runner.submit("comments", video_id=VIDEO_ID, mode="incremental")
        finished = wait_for_job(store, job["id"])
    finally:
        runner.close()

    assert finished["status"] == "blocked"
    assert finished["result"]["export_count"] == 1
    assert finished["result"]["file"].endswith("blocked-snapshot.jsonl")
    assert "已保留截至失败点的 1 条有效记录" in finished["message"]
    assert "未更新增量基线" in finished["message"]
    external_file = Path(finished["result"]["export_file"])
    assert external_file.is_file()
    exported = [
        json.loads(line)
        for line in external_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["comment_id"] for record in exported] == ["comment-new"]
    assert json.loads(canonical_path.read_text(encoding="utf-8"))["comment_id"] == (
        "comment-old"
    )
    manifest_path = settings.data_home / finished["result"]["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["usable_as_baseline"] is False
    assert manifest["incremental_file"] is None
    assert manifest["export_file"].endswith("blocked-snapshot.jsonl")
    video = store.get_video(VIDEO_ID)
    assert video["comment_count"] == 1
    assert video["last_comment_export_at"] == "2026-09-07T01:00:00Z"


class FakeBody:
    def inner_text(self, *, timeout: int) -> str:
        assert timeout > 0
        return "测试创作者\n抖音号：creator-handle"


class FakePage:
    def __init__(self) -> None:
        self.home_visits = 0

    def goto(self, *_args: Any, **_kwargs: Any) -> None:
        if _args and _args[0] == "https://creator.douyin.com/creator-micro/home":
            self.home_visits += 1
        return None

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def locator(self, selector: str) -> FakeBody:
        assert selector == "body"
        return FakeBody()


class DelayedBody:
    def __init__(self) -> None:
        self.calls = 0

    def inner_text(self, *, timeout: int) -> str:
        assert timeout > 0
        self.calls += 1
        if self.calls < 3:
            return "加载中，请稍候..."
        return "测试创作者\n抖音号：creator-handle"


class DelayedIdentityPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.body = DelayedBody()
        self.waits = 0

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 500
        self.waits += 1

    def locator(self, selector: str) -> DelayedBody:
        assert selector == "body"
        return self.body


def test_export_identity_verification_waits_for_creator_home_to_load(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.set_meta("creator_identity", identity().as_dict())
    runner = LocalJobRunner(store, settings)
    page = DelayedIdentityPage()

    runner._verify_page_identity(page)

    assert page.body.calls == 3
    assert page.waits == 2


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]

    def close(self) -> None:
        return None


class FakePlaywrightManager:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


def test_complete_export_includes_account_and_video_manifests_without_mysql(
    tmp_path: Path, monkeypatch
) -> None:
    settings = make_settings(tmp_path)
    store = LocalStore(settings.database_path)
    store.set_meta("creator_identity", identity().as_dict())
    store.set_meta(
        "last_video_sync",
        {
            "finished_at": "2026-09-07T11:59:00Z",
            "summary": "发现 0 个视频",
            "complete": True,
        },
    )
    store.upsert_videos(
        [
            {
                "video_id": VIDEO_ID,
                "title": "测试视频",
                "video_url": f"https://www.douyin.com/video/{VIDEO_ID}",
                "manifest_path": f"works/videos/douyin/{VIDEO_ID}/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
            }
        ]
    )

    import playwright.sync_api
    import admin_app.local_jobs as jobs_module

    monkeypatch.setattr(
        playwright.sync_api, "sync_playwright", lambda: FakePlaywrightManager()
    )
    context = FakeContext()
    monkeypatch.setattr(
        jobs_module,
        "launch_persistent_context",
        lambda _playwright, _profile: context,
    )

    def fake_export(output_root: Path, *, specs: Any, **_kwargs: Any) -> ExportRunResult:
        run = Path(output_root) / "2026-09-07_12-00-00"
        run.mkdir(parents=True)
        manifest = run / "manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        files = tuple(
            ExportFileResult(
                spec=spec,
                status="succeeded",
                started_at="2026-09-07T12:00:00+08:00",
                finished_at="2026-09-07T12:00:01+08:00",
                file_path=run / spec.filename,
                size=1,
                sha256="0" * 64,
            )
            for spec in specs
        )
        return ExportRunResult(
            run_directory=run,
            manifest_path=manifest,
            status="succeeded",
            started_at="2026-09-07T12:00:00+08:00",
            finished_at="2026-09-07T12:00:01+08:00",
            files=files,
        )

    runner = LocalJobRunner(store, settings, exporter=fake_export)
    status, message, result = runner._run_export()
    assert status == "succeeded"
    assert message == "完成 12/12 个表格"
    assert result["manifest"].endswith("complete-manifest.json")
    aggregate = json.loads(
        (settings.data_home / result["manifest"]).read_text(encoding="utf-8")
    )
    assert aggregate["account_handle"] == "creator-handle"
    assert aggregate["video_count"] == 1
    assert aggregate["total"] == 12
    assert context.pages[0].home_visits == 2
