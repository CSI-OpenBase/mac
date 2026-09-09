"""Strictly operator-triggered jobs for the local archive application."""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .collector import CollectionResult, collect_video
from .douyin_exports import (
    DEFAULT_EXPORT_SPECS,
    ExportRunResult,
    export_creator_data,
    launch_persistent_context,
)
from .local_browser import (
    AuthorizationBlocked,
    _identity_from_text,
    authorize_creator,
)
from .local_config import LocalSettings
from .local_store import JobStateConflictError, LocalStore, utc_now
from .video_archive import (
    VIDEO_ID_RE,
    VideoArchiveIdentityError,
    VideoArchiveResult,
    sync_profile_videos,
)


TaskFunction = Callable[..., Any]


class LocalJobBlocked(RuntimeError):
    pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _allocate_timestamp_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    for index in range(10_000):
        suffix = "" if index == 0 else f"_{index:02d}"
        target = root / f"{base}{suffix}"
        try:
            target.mkdir()
        except FileExistsError:
            continue
        return target
    raise FileExistsError(f"could not allocate a run directory under {root}")


def _export_summary(result: ExportRunResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "directory": str(result.run_directory),
        "manifest": str(result.manifest_path),
        "succeeded": result.succeeded_count,
        "blocked": result.blocked_count,
        "failed": sum(item.status == "failed" for item in result.files),
        "total": len(result.files),
    }


class LocalJobRunner:
    """One daemon worker; no scheduled or restart-resumed work is supported."""

    def __init__(
        self,
        store: LocalStore,
        settings: LocalSettings,
        *,
        authorize: TaskFunction = authorize_creator,
        exporter: TaskFunction = export_creator_data,
        video_sync: TaskFunction = sync_profile_videos,
        comment_collector: TaskFunction = collect_video,
    ) -> None:
        self.store = store
        self.settings = settings
        self.authorize = authorize
        self.exporter = exporter
        self.video_sync = video_sync
        self.comment_collector = comment_collector
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._force_interrupted = threading.Event()
        self._state_lock = threading.RLock()
        self._shutdown_result: bool | None = None

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            if self._closed.is_set():
                raise RuntimeError("the local job runner is closed")
            self._thread = threading.Thread(
                target=self._run_loop,
                name="csi-openbase-local-worker",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> bool:
        with self._state_lock:
            if self._shutdown_result is not None:
                return self._shutdown_result
            self._closed.set()
            self._queue.put(None)
            thread = self._thread
            if thread and thread.is_alive():
                thread.join(timeout=2)
            stopped = not (thread and thread.is_alive())
            if not stopped:
                # Persist the terminal state before the desktop host terminates
                # the backend/browser process tree.
                self._force_interrupted.set()
            self.store.interrupt_active_jobs()
            self._shutdown_result = stopped
            return stopped

    def submit(self, kind: str, **payload: Any) -> dict[str, Any]:
        with self._state_lock:
            if self._closed.is_set():
                raise RuntimeError("the local job runner is closed")
            video_id = str(payload.get("video_id") or "").strip() or None
            if kind == "comments":
                if not video_id or self.store.get_video(video_id) is None:
                    raise KeyError("video does not exist in the local archive")
                payload = {**payload, "trigger": "user"}
            job = self.store.create_job(
                kind, video_id=video_id, payload=payload
            )
            self.start()
            self._queue.put(int(job["id"]))
            return job

    def _run_loop(self) -> None:
        while not self._closed.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            job = self.store.get_job(job_id)
            if job is None or job["status"] != "queued":
                continue
            try:
                self.store.update_job(
                    job_id, "running", message="正在执行", expected_status="queued"
                )
            except JobStateConflictError:
                continue
            try:
                status, message, result = self._execute(job)
            except (
                AuthorizationBlocked,
                LocalJobBlocked,
                VideoArchiveIdentityError,
            ) as exc:
                self._finish_job(job_id, "blocked", message=str(exc))
            except Exception as exc:
                self._finish_job(
                    job_id,
                    "failed",
                    message=f"{type(exc).__name__}: {str(exc)[:600]}",
                )
            else:
                self._finish_job(
                    job_id, status, message=message, result=result
                )

    def _finish_job(
        self,
        job_id: int,
        status: str,
        *,
        message: str,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        with suppress(JobStateConflictError):
            self.store.update_job(
                job_id,
                status,
                message=message,
                result=result,
                expected_status="running",
            )

    def _execute(
        self, job: Mapping[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        payload = dict(job.get("payload") or {})
        kind = str(job["kind"])
        if kind == "authorize":
            return self._run_authorization()
        if kind == "export":
            return self._run_export()
        if kind == "sync_videos":
            return self._run_video_sync(payload)
        if kind == "comments":
            return self._run_comments(str(job.get("video_id") or ""))
        raise ValueError(f"unsupported local job kind: {kind}")

    def _expected_handle(self) -> str | None:
        identity = self.store.get_meta("creator_identity", {})
        return str(identity.get("handle") or "").strip() or None

    def _raise_if_force_interrupted(self) -> None:
        if self._force_interrupted.is_set():
            raise RuntimeError("local job was interrupted during shutdown")

    def _run_authorization(self) -> tuple[str, str, dict[str, Any]]:
        identity = self.authorize(
            browser_profile_dir=self.settings.browser_profile_dir,
            timeout_seconds=self.settings.authorization_timeout_seconds,
            expected_handle=self._expected_handle(),
        )
        self._raise_if_force_interrupted()
        value = identity.as_dict() if hasattr(identity, "as_dict") else asdict(identity)
        self.store.set_meta("creator_identity", value)
        return "succeeded", f"已连接抖音号 {value['handle']}", value

    def _verify_page_identity(self, page: Any) -> None:
        expected = self._expected_handle()
        if not expected:
            raise AuthorizationBlocked("creator authorization is required")
        page.goto(
            "https://creator.douyin.com/creator-micro/home",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        deadline = time.monotonic() + 30
        observed_handle = ""
        while time.monotonic() < deadline:
            current_url = str(getattr(page, "url", "")).casefold()
            if any(token in current_url for token in ("passport", "captcha", "verify")):
                raise AuthorizationBlocked("complete creator login in the opened browser")
            try:
                text = page.locator("body").inner_text(timeout=5_000)
            except Exception:
                text = ""
            identity = _identity_from_text(text, authorized_at=utc_now())
            if identity is not None:
                observed_handle = identity.handle
                if identity.handle == expected:
                    return
            page.wait_for_timeout(500)
        if observed_handle:
            raise AuthorizationBlocked(
                "the signed-in creator does not match this workspace"
            )
        raise AuthorizationBlocked("complete creator login in the opened browser")

    def _run_export(self) -> tuple[str, str, dict[str, Any]]:
        last_video_sync = self.store.get_meta("last_video_sync", {})
        if not last_video_sync:
            raise LocalJobBlocked("sync the signed-in profile before exporting all tables")
        if last_video_sync.get("complete") is not True:
            raise LocalJobBlocked(
                "the profile archive is incomplete; sync it again before exporting all tables"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LocalJobBlocked("Playwright and Chromium are required") from exc

        account_specs = tuple(
            spec for spec in DEFAULT_EXPORT_SPECS if spec.category != "work-detail"
        )
        detail_specs = tuple(
            spec for spec in DEFAULT_EXPORT_SPECS if spec.category == "work-detail"
        )
        video_rows = self.store.list_videos(limit=100_000)
        all_runs: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            context = launch_persistent_context(
                playwright, self.settings.browser_profile_dir
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._verify_page_identity(page)
                account_run = self.exporter(
                    self.settings.exports_dir,
                    specs=account_specs,
                    page=page,
                )
                self._raise_if_force_interrupted()
                all_runs.append({"scope": "account", **_export_summary(account_run)})
                detail_root = account_run.run_directory / "videos"
                for video in video_rows:
                    self._raise_if_force_interrupted()
                    # A complete export can run for a long time. Re-check the
                    # workspace owner before every video's detail batch so an
                    # account switch cannot silently mix creator archives.
                    self._verify_page_identity(page)
                    video_id = str(video["video_id"])
                    if not VIDEO_ID_RE.fullmatch(video_id):
                        raise ValueError("video_id must contain 8-32 digits")
                    run = self.exporter(
                        detail_root / video_id,
                        specs=detail_specs,
                        variables={"video_id": video_id},
                        page=page,
                    )
                    self._raise_if_force_interrupted()
                    all_runs.append(
                        {"scope": "video", "video_id": video_id, **_export_summary(run)}
                    )
            finally:
                context.close()

        succeeded = sum(int(run["succeeded"]) for run in all_runs)
        failed = sum(int(run["failed"]) for run in all_runs)
        blocked = sum(int(run["blocked"]) for run in all_runs)
        total = sum(int(run["total"]) for run in all_runs)
        aggregate = {
            "schema": "csi-openbase.complete-export-run",
            "version": 1,
            "started_at": all_runs[0]["started_at"],
            "finished_at": all_runs[-1]["finished_at"],
            "account_handle": self._expected_handle(),
            "video_count": len(video_rows),
            "succeeded": succeeded,
            "failed": failed,
            "blocked": blocked,
            "total": total,
            "runs": all_runs,
        }
        aggregate_path = account_run.run_directory / "complete-manifest.json"
        self._raise_if_force_interrupted()
        _atomic_json(aggregate_path, aggregate)
        status = "succeeded" if succeeded == total else ("partial" if succeeded else "blocked" if blocked else "failed")
        summary = f"完成 {succeeded}/{total} 个表格"
        meta = {
            "finished_at": aggregate["finished_at"],
            "summary": summary,
            "directory": _relative(account_run.run_directory, self.settings.data_home),
            "manifest": _relative(aggregate_path, self.settings.data_home),
        }
        self.store.set_meta("last_export", meta)
        return status, summary, {**meta, "counts": aggregate}

    def _run_video_sync(
        self, _payload: Mapping[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        expected = self._expected_handle()
        if not expected:
            raise AuthorizationBlocked("creator authorization is required")
        # Revalidate creator-center identity before using the shared login profile.
        self.authorize(
            browser_profile_dir=self.settings.browser_profile_dir,
            timeout_seconds=max(30, min(self.settings.authorization_timeout_seconds, 90)),
            expected_handle=expected,
        )
        result: VideoArchiveResult = self.video_sync(
            profile_url="https://www.douyin.com/user/self",
            works_dir=self.settings.works_dir,
            browser_profile_dir=self.settings.browser_profile_dir,
            download_covers=True,
            expected_handle=expected,
        )
        self._raise_if_force_interrupted()
        index_records: list[dict[str, Any]] = []
        for record in result.videos:
            video_id = str(record["video_id"])
            visible_metrics = record.get("visible_metrics")
            visible_comment_count = (
                visible_metrics.get("comment_count")
                if isinstance(visible_metrics, Mapping)
                else None
            )
            manifest_path = (
                self.settings.works_dir
                / "videos"
                / "douyin"
                / video_id
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            cover = manifest.get("cover") or {}
            index_records.append(
                {
                    "video_id": video_id,
                    "platform": "douyin",
                    "title": str(manifest.get("title") or record.get("title") or video_id),
                    "video_url": str(manifest["url"]),
                    "cover_path": (
                        _relative(manifest_path.parent / str(cover["path"]), self.settings.data_home)
                        if cover.get("path")
                        else ""
                    ),
                    "manifest_path": _relative(manifest_path, self.settings.data_home),
                    "first_seen_at": str(manifest["first_seen"]),
                    "last_seen_at": str(manifest["last_seen"]),
                    "visible_comment_count": visible_comment_count,
                }
            )
        self.store.upsert_videos(index_records)
        summary = (
            f"发现 {result.discovered_count} 个视频，"
            f"新建 {len(result.created_video_ids)} 个档案"
        )
        meta = {
            "finished_at": result.discovered_at,
            "summary": summary,
            "directory": _relative(result.discovery_dir, self.settings.data_home),
            "warnings": list(result.warnings),
            "declared_work_count": result.declared_work_count,
            "captured_work_count": result.captured_work_count,
            "complete": result.listing_complete,
        }
        self.store.set_meta("last_video_sync", meta)
        status = (
            "partial"
            if result.warnings or not result.listing_complete
            else "succeeded"
        )
        return status, summary, meta

    def _run_comments(self, video_id: str) -> tuple[str, str, dict[str, Any]]:
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("video_id must contain 8-32 digits")
        video = self.store.get_video(video_id)
        if video is None:
            raise KeyError("video does not exist in the local archive")
        comments_root = (
            self.settings.works_dir / "videos" / "douyin" / video_id / "comments"
        )
        run_directory = _allocate_timestamp_dir(comments_root)
        result: CollectionResult = self.comment_collector(
            video_id=video_id,
            video_url=str(video["video_url"]),
            video_title=str(video["title"]),
            batches_dir=run_directory,
            browser_profile_dir=self.settings.browser_profile_dir,
            capture_seconds=self.settings.browser_capture_seconds,
            trigger="manual",
        )
        self._raise_if_force_interrupted()
        count = len(result.records)
        exported_at = (
            str(result.records[0].get("collected_at")) if result.records else utc_now()
        )
        if result.status in {"complete", "partial"}:
            self.store.record_comment_export(
                video_id, count=count, exported_at=exported_at
            )
        payload = {
            "video_id": video_id,
            "status": result.status,
            "count": count,
            "directory": _relative(run_directory, self.settings.data_home),
            "file": (
                _relative(result.batch_path, self.settings.data_home)
                if result.batch_path
                else None
            ),
            "diagnostics": dict(result.diagnostics),
        }
        status = result.status if result.status in {"partial", "blocked"} else "succeeded"
        return status, result.message, payload
