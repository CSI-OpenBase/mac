"""Strictly operator-triggered jobs for the local archive application."""

from __future__ import annotations

import json
import os
import queue
import shutil
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
from .local_config import (
    COMMENT_EXPORT_DIRECTORY_KEY,
    LOCAL_PREFERENCES_META_KEY,
    LocalSettings,
    prepare_comment_export_directory,
)
from .local_store import JobStateConflictError, LocalStore, utc_now
from .time_utils import beijing_slug
from .video_archive import (
    CONTENT_MANAGEMENT_URL,
    VIDEO_ID_RE,
    VideoArchiveIdentityError,
    VideoArchiveResult,
    fetch_video_comment_count,
    sync_profile_videos,
)


TaskFunction = Callable[..., Any]
COMMENT_EXPORT_MODES = frozenset({"incremental", "full"})
COMMENT_INDEX_FILENAME = "comments.jsonl"
COMMENT_MANIFEST_FILENAME = "manifest.json"
COMMENT_VOLATILE_FIELDS = frozenset({"collected_at", "collection_batch"})


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


def _atomic_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                json.dump(
                    dict(record),
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _comment_key(record: Mapping[str, Any]) -> tuple[str, str]:
    platform = str(record.get("platform") or "douyin").strip()
    comment_id = str(record.get("comment_id") or "").strip()
    if not platform or not comment_id:
        raise LocalJobBlocked("已有评论基线缺少 platform 或 comment_id")
    return platform, comment_id


def _load_comment_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"line {line_number} is not an object")
                _comment_key(value)
                records.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LocalJobBlocked(f"无法读取已有评论基线 {path.name}：{exc}") from exc
    return records


def _historical_snapshot(comments_root: Path) -> Path | None:
    if not comments_root.is_dir():
        return None
    run_directories = sorted(
        (
            path
            for path in comments_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for run_directory in run_directories:
        manifest_path = run_directory / COMMENT_MANIFEST_FILENAME
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(manifest, dict)
                or manifest.get("usable_as_baseline") is False
            ):
                continue
            snapshot_name = str(manifest.get("snapshot_file") or "").strip()
            if snapshot_name and Path(snapshot_name).name == snapshot_name:
                candidate = run_directory / snapshot_name
                if candidate.is_file() and not candidate.is_symlink():
                    return candidate
        candidates = sorted(
            (
                path
                for path in run_directory.glob("*.jsonl")
                if path.is_file()
                and not path.is_symlink()
                and "incremental" not in path.stem.casefold()
            ),
            key=lambda path: path.name,
        )
        if candidates:
            return candidates[0]
    return None


def _load_comment_baseline(
    comments_root: Path,
) -> tuple[list[dict[str, Any]], Path | None]:
    canonical_path = comments_root / COMMENT_INDEX_FILENAME
    if canonical_path.is_file() and not canonical_path.is_symlink():
        return _load_comment_records(canonical_path), canonical_path
    historical_path = _historical_snapshot(comments_root)
    if historical_path is None:
        return [], None
    return _load_comment_records(historical_path), historical_path


def _comment_state(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in COMMENT_VOLATILE_FIELDS
    }


def _comment_increment(
    baseline: Iterable[Mapping[str, Any]],
    observed: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    baseline_order: list[tuple[str, str]] = []
    baseline_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in baseline:
        record = dict(raw)
        key = _comment_key(record)
        if key not in baseline_by_key:
            baseline_order.append(key)
        baseline_by_key[key] = record

    observed_order: list[tuple[str, str]] = []
    observed_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in observed:
        record = dict(raw)
        key = _comment_key(record)
        if key not in observed_by_key:
            observed_order.append(key)
        observed_by_key[key] = record

    new_keys = {key for key in observed_order if key not in baseline_by_key}
    updated_keys = {
        key
        for key in observed_order
        if key in baseline_by_key
        and _comment_state(observed_by_key[key])
        != _comment_state(baseline_by_key[key])
    }
    canonical_order = [*baseline_order]
    canonical_by_key = dict(baseline_by_key)
    for key in observed_order:
        if key not in canonical_by_key:
            canonical_order.append(key)
        canonical_by_key[key] = observed_by_key[key]
    canonical = [canonical_by_key[key] for key in canonical_order]

    change_keys = new_keys | updated_keys
    delivery_keys: list[tuple[str, str]] = []
    delivered: set[tuple[str, str]] = set()
    visiting: set[tuple[str, str]] = set()

    def append_with_ancestry(key: tuple[str, str]) -> None:
        if key in delivered:
            return
        if key in visiting:
            raise LocalJobBlocked("评论增量基线包含循环父子关系")
        visiting.add(key)
        record = canonical_by_key[key]
        platform = key[0]
        for field in ("root_comment_id", "parent_comment_id"):
            related_id = str(record.get(field) or "").strip()
            related_key = (platform, related_id)
            if related_id and related_key in canonical_by_key:
                append_with_ancestry(related_key)
        visiting.remove(key)
        delivered.add(key)
        delivery_keys.append(key)

    for key in observed_order:
        if key in change_keys:
            append_with_ancestry(key)
    delta = [canonical_by_key[key] for key in delivery_keys]
    stats = {
        "baseline_count": len(baseline_by_key),
        "snapshot_count": len(observed_by_key),
        "new_count": len(new_keys),
        "updated_count": len(updated_keys),
        "unchanged_count": len(observed_by_key) - len(new_keys) - len(updated_keys),
        "context_count": len(delivery_keys) - len(change_keys),
        "not_observed_count": len(set(baseline_by_key) - set(observed_by_key)),
        "canonical_count": len(canonical_by_key),
    }
    return delta, canonical, stats


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _allocate_timestamp_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = beijing_slug()
    for index in range(10_000):
        suffix = "" if index == 0 else f"_{index:02d}"
        target = root / f"{base}{suffix}"
        try:
            target.mkdir()
        except FileExistsError:
            continue
        return target
    raise FileExistsError(f"could not allocate a run directory under {root}")


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary_name)
        os.replace(temporary_name, destination)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


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
        comment_counter: TaskFunction = fetch_video_comment_count,
        comment_collector: TaskFunction = collect_video,
    ) -> None:
        self.store = store
        self.settings = settings
        self.authorize = authorize
        self.exporter = exporter
        self.video_sync = video_sync
        self.comment_counter = comment_counter
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
            if kind in {"comments", "comment_count"}:
                if not video_id or self.store.get_video(video_id) is None:
                    raise KeyError("video does not exist in the local archive")
                payload = {**payload, "trigger": "user"}
            if kind == "comments":
                mode = str(payload.get("mode") or "incremental")
                if mode not in COMMENT_EXPORT_MODES:
                    raise ValueError("comment export mode must be incremental or full")
                payload = {**payload, "mode": mode}
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
        if kind == "comment_count":
            return self._run_comment_count(str(job.get("video_id") or ""))
        if kind == "comments":
            return self._run_comments(
                str(job.get("video_id") or ""),
                mode=str(payload.get("mode") or "incremental"),
            )
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
            profile_url=CONTENT_MANAGEMENT_URL,
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
                    "published_at": record.get("published_at"),
                    "cover_path": (
                        _relative(manifest_path.parent / str(cover["path"]), self.settings.data_home)
                        if cover.get("path")
                        else ""
                    ),
                    "manifest_path": _relative(manifest_path, self.settings.data_home),
                    "first_seen_at": str(manifest["first_seen"]),
                    "last_seen_at": str(manifest["last_seen"]),
                    "visible_comment_count": visible_comment_count,
                    "platform_groups": list(record.get("platform_groups") or ()),
                    "platform_groups_observed": "response" in record.get("sources", ()),
                }
            )
        self.store.upsert_videos(index_records)
        if result.listing_complete:
            self.store.prune_empty_platform_groups(platform="douyin")
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

    def _run_comment_count(
        self, video_id: str
    ) -> tuple[str, str, dict[str, Any]]:
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("video_id must contain 8-32 digits")
        video = self.store.get_video(video_id)
        if video is None:
            raise KeyError("video does not exist in the local archive")
        count = self.comment_counter(
            video_id=video_id,
            video_url=str(video["video_url"]),
            browser_profile_dir=self.settings.browser_profile_dir,
            timeout_seconds=max(10, self.settings.browser_capture_seconds),
        )
        self._raise_if_force_interrupted()
        result = self.store.record_visible_comment_count(video_id, count=count)
        delta = result["delta"]
        change = "首次获取" if delta is None else f"较上次 {delta:+,}"
        message = f"评论数 {result['current_count']:,}，{change}"
        return "succeeded", message, {**result, "content_exported": False}

    def _run_comments(
        self, video_id: str, *, mode: str
    ) -> tuple[str, str, dict[str, Any]]:
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("video_id must contain 8-32 digits")
        if mode not in COMMENT_EXPORT_MODES:
            raise ValueError("comment export mode must be incremental or full")
        video = self.store.get_video(video_id)
        if video is None:
            raise KeyError("video does not exist in the local archive")
        comments_root = (
            self.settings.works_dir / "videos" / "douyin" / video_id / "comments"
        )
        baseline, baseline_path = _load_comment_baseline(comments_root)
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
        snapshot_records = [dict(record) for record in result.records]
        delta_records, canonical_records, increment = _comment_increment(
            baseline, snapshot_records
        )
        count = len(snapshot_records)
        exported_at = (
            str(snapshot_records[0].get("collected_at"))
            if snapshot_records
            else utc_now()
        )
        usable = result.status in {"complete", "partial"}
        snapshot_path = result.batch_path
        if usable and snapshot_path is None:
            snapshot_path = run_directory / "comments-full.jsonl"
            _atomic_jsonl(snapshot_path, snapshot_records)

        delta_path: Path | None = None
        export_source = snapshot_path
        if usable:
            _atomic_jsonl(comments_root / COMMENT_INDEX_FILENAME, canonical_records)
            if mode == "incremental":
                snapshot_stem = snapshot_path.stem if snapshot_path else "comments"
                delta_path = run_directory / f"{snapshot_stem}-incremental.jsonl"
                _atomic_jsonl(delta_path, delta_records)
                export_source = delta_path
            self.store.record_comment_export(
                video_id, count=count, exported_at=exported_at
            )

        manifest_path = run_directory / COMMENT_MANIFEST_FILENAME
        _atomic_json(
            manifest_path,
            {
                "schema_version": 1,
                "video_id": video_id,
                "mode": mode,
                "status": result.status,
                "usable_as_baseline": usable,
                "baseline_file": (
                    _relative(baseline_path, comments_root) if baseline_path else None
                ),
                "snapshot_file": snapshot_path.name if snapshot_path else None,
                "incremental_file": delta_path.name if delta_path else None,
                "export_file": export_source.name if export_source and usable else None,
                "stats": increment,
                "diagnostics": dict(result.diagnostics),
            },
        )
        external_file: Path | None = None
        export_warning = ""
        preferences = self.store.get_meta(LOCAL_PREFERENCES_META_KEY, {})
        configured_directory = (
            preferences.get(COMMENT_EXPORT_DIRECTORY_KEY)
            if isinstance(preferences, Mapping)
            else None
        )
        if export_source and usable:
            try:
                export_root = prepare_comment_export_directory(
                    str(configured_directory or ""), self.settings
                )
                if export_root is not None:
                    external_file = (
                        export_root
                        / video_id
                        / run_directory.name
                        / export_source.name
                    )
                    _copy_file_atomic(export_source, external_file)
            except (OSError, ValueError) as exc:
                export_warning = f"；外部目录写入失败：{exc}"
        payload = {
            "video_id": video_id,
            "status": result.status,
            "mode": mode,
            "count": count,
            "export_count": len(delta_records) if mode == "incremental" else count,
            **increment,
            "directory": _relative(run_directory, self.settings.data_home),
            "file": (
                _relative(export_source, self.settings.data_home)
                if export_source and usable
                else None
            ),
            "snapshot_file": (
                _relative(snapshot_path, self.settings.data_home)
                if snapshot_path
                else None
            ),
            "manifest": _relative(manifest_path, self.settings.data_home),
            "export_file": str(external_file) if external_file else None,
            "diagnostics": dict(result.diagnostics),
        }
        status = result.status if result.status in {"partial", "blocked"} else "succeeded"
        message = result.message
        if usable:
            if mode == "incremental":
                message = (
                    f"{message}；增量新增 {increment['new_count']} 条，"
                    f"更新 {increment['updated_count']} 条，"
                    f"未变化 {increment['unchanged_count']} 条"
                )
                if increment["context_count"]:
                    message = (
                        f"{message}，附带关系上下文 {increment['context_count']} 条"
                    )
            else:
                message = (
                    f"{message}；完整同步 {count} 条，"
                    f"其中新增 {increment['new_count']} 条、"
                    f"更新 {increment['updated_count']} 条"
                )
            if increment["not_observed_count"]:
                qualifier = (
                    "本次未观察到（采集不完整）"
                    if result.status == "partial"
                    else "平台当前不可见"
                )
                message = (
                    f"{message}；{increment['not_observed_count']} 条历史评论"
                    f"{qualifier}，未从本地索引删除"
                )
        if external_file:
            message = f"{message}；已导出到 {external_file.parent}"
        elif export_warning:
            status = "partial" if status == "succeeded" else status
            message = f"{message}{export_warning}"
        return status, message, payload
