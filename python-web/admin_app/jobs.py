"""Synchronous job execution and APScheduler wiring for the local worker."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .archive_lock import archive_lock
from .account_analysis import generate_account_analysis
from .creator_store import sync_creator_sources
from .collector import CollectionResult, collect_video, update_collection_metadata
from .config import Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

from scripts import analyze_comments, import_comments_mysql, ingest_comments


class JobRepository(Protocol):
    """The atomic state-transition surface required by the worker."""

    def claim_next_job(
        self, worker_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None: ...

    def heartbeat_job(self, job_id: int, worker_id: str) -> bool: ...

    def mark_job_succeeded(
        self,
        job_id: int,
        result: Mapping[str, Any] | None = None,
        message: str | None = None,
        worker_id: str | None = None,
    ) -> bool: ...

    def mark_job_blocked(
        self,
        job_id: int,
        message: str,
        result: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> bool: ...

    def mark_job_failed(
        self,
        job_id: int,
        message: str,
        worker_id: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class JobExecution:
    status: str
    message: str
    result: Mapping[str, Any]


class BlockedJob(RuntimeError):
    def __init__(self, message: str, result: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = dict(result or {})


class WorkerAlreadyRunning(RuntimeError):
    pass


class WorkerInstanceLock:
    """Cross-process advisory file lock used in addition to APScheduler max_instances."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._handle: io.BufferedRandom | None = None

    def __enter__(self) -> "WorkerInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows hosts
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise WorkerAlreadyRunning(
                f"another collection worker holds {self.path}"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, *_: Any) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows hosts
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class _Heartbeat:
    def __init__(
        self,
        repository: JobRepository,
        *,
        job_id: int,
        worker_id: str,
        interval_seconds: int = 15,
    ) -> None:
        self.repository = repository
        self.job_id = job_id
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Heartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-heartbeat-{self.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not self.repository.heartbeat_job(self.job_id, self.worker_id):
                    return
            except Exception:
                # A transient heartbeat failure must not execute the job twice;
                # the repository's running claim remains authoritative.
                continue

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _payload(job: Mapping[str, Any]) -> dict[str, Any]:
    value = job.get("payload") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("job payload is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("job payload must be a JSON object")
    return dict(value)


def _inside_repository(
    value: Any, *, default: Path, repository_root: Path = REPOSITORY_ROOT
) -> Path:
    path = default if value in (None, "") else Path(str(value))
    if not path.is_absolute():
        path = repository_root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise ValueError(f"job path must stay inside the allowed data root: {path}") from exc
    return resolved


def _redact(message: str, secret: str) -> str:
    sanitized = message.replace(secret, "[redacted]") if secret else message
    return sanitized[:16_000]


class JobWorker:
    def __init__(
        self,
        repository: JobRepository,
        settings: Settings,
        *,
        worker_id: str | None = None,
        collector: Callable[..., CollectionResult] = collect_video,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.worker_id = worker_id or make_worker_id()
        self.collector = collector

    def run_once(self) -> bool:
        """Claim and run at most one due job. Return whether one was claimed."""
        job = self.repository.claim_next_job(self.worker_id)
        if job is None:
            return False
        job_id = int(job["id"])
        try:
            with _Heartbeat(
                self.repository, job_id=job_id, worker_id=self.worker_id
            ):
                execution = self.execute(job)
            if execution.status not in {"succeeded", "blocked"}:
                raise RuntimeError(
                    f"handler returned unsupported status: {execution.status!r}"
                )
            final_message = _redact(execution.message, self.settings.db_password)
            if execution.status == "blocked":
                updated = self.repository.mark_job_blocked(
                    job_id,
                    final_message,
                    result=execution.result,
                    worker_id=self.worker_id,
                )
            else:
                updated = self.repository.mark_job_succeeded(
                    job_id,
                    result=execution.result,
                    message=final_message,
                    worker_id=self.worker_id,
                )
            if not updated:
                raise RuntimeError(
                    f"job {job_id} was no longer owned by worker {self.worker_id}"
                )
        except BlockedJob as exc:
            self.repository.mark_job_blocked(
                job_id,
                _redact(str(exc), self.settings.db_password),
                result=exc.result,
                worker_id=self.worker_id,
            )
        except Exception as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", self.settings.db_password)
            self.repository.mark_job_failed(
                job_id, message, worker_id=self.worker_id
            )
        return True

    def execute(self, job: Mapping[str, Any]) -> JobExecution:
        handlers: dict[str, Callable[[Mapping[str, Any]], JobExecution]] = {
            "analyze": self._analyze,
            "sync_mysql": self._sync_mysql_job,
            "collect_video": self._collect_video,
        }
        job_type = str(job.get("job_type") or "")
        handler = handlers.get(job_type)
        if handler is None:
            raise ValueError(f"unsupported job_type: {job_type!r}")
        return handler(job)

    def _analyze(self, job: Mapping[str, Any]) -> JobExecution:
        payload = _payload(job)
        input_path = _inside_repository(
            payload.get("input"),
            default=self.settings.canonical_comments_path,
            repository_root=self.settings.data_dir,
        )
        rules_path = _inside_repository(
            payload.get("rules"),
            default=self.settings.theme_rules_path,
            repository_root=self.settings.data_dir,
        )
        output_path = _inside_repository(
            payload.get("output"),
            default=self.settings.reports_dir / "comment-insights.md",
            repository_root=self.settings.data_dir,
        )
        argv = [
            "--input",
            str(input_path),
            "--rules",
            str(rules_path),
            "--output",
            str(output_path),
            "--workspace-name",
            self.settings.workspace_name,
            "--platform",
            self.settings.platform,
        ]
        for name in ("term_limit", "question_limit", "min_frequency"):
            if name in payload:
                value = int(payload[name])
                if value < 1:
                    raise ValueError(f"{name} must be positive")
                argv.extend([f"--{name.replace('_', '-')}", str(value)])
        if payload.get("cohort_title"):
            argv.extend(["--cohort-title", str(payload["cohort_title"])])
        with archive_lock(self.settings.comments_dir / ".archive.lock"):
            if analyze_comments.main(argv) != 0:
                raise RuntimeError("comment analysis rejected the current archive")
            records = analyze_comments.load_records([input_path])
            account_analysis, account_report = generate_account_analysis(self.settings)
        return JobExecution(
            status="succeeded",
            message=(
                "Workspace analysis generated from "
                f"{account_analysis['works']['analyzed_work_count']} works and "
                f"{len(records)} comments"
            ),
            result={
                "comment_count": len(records),
                "work_count": account_analysis["works"]["analyzed_work_count"],
                "output": output_path.relative_to(self.settings.data_dir.resolve()).as_posix(),
                "account_report": account_report.relative_to(
                    self.settings.data_dir.resolve()
                ).as_posix(),
            },
        )

    def _sync_mysql_job(self, job: Mapping[str, Any]) -> JobExecution:
        payload = _payload(job)
        with archive_lock(self.settings.comments_dir / ".archive.lock"):
            stats, verification = self._sync_mysql(payload)
        return JobExecution(
            status="succeeded",
            message=(
                f"MySQL synchronized: {stats['canonical_comment_count']} comments, "
                f"{stats['inserted_snapshot_count']} new snapshots"
            ),
            result={"import": stats, "verification": verification},
        )

    def _sync_mysql(
        self, payload: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = payload or {}
        comments_path = _inside_repository(
            payload.get("comments"),
            default=self.settings.canonical_comments_path,
            repository_root=self.settings.data_dir,
        )
        targets_path = _inside_repository(
            payload.get("targets"),
            default=self.settings.targets_path,
            repository_root=self.settings.data_dir,
        )
        progress_path = _inside_repository(
            payload.get("progress"),
            default=self.settings.progress_path,
            repository_root=self.settings.data_dir,
        )
        import_payload = import_comments_mysql.load_import_payload(
            comments_path, targets_path, progress_path
        )
        import_comments_mysql.create_database_if_missing(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
        )
        connection = import_comments_mysql.connect_mysql(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
        )
        try:
            connection.begin()
            try:
                stats, _ = import_comments_mysql.import_payload(
                    connection,
                    import_payload,
                    comments_path=comments_path,
                    targets_path=targets_path,
                    progress_path=progress_path,
                    manage_transaction=False,
                    verify=False,
                )
                stats["creator_data"] = sync_creator_sources(
                    connection, self.settings, manage_transaction=False
                )
                verification = import_comments_mysql.verify_database(connection)
                connection.commit()
                return stats, verification
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _collect_video(self, job: Mapping[str, Any]) -> JobExecution:
        payload = _payload(job)
        video_id = str(job.get("video_id") or payload.get("video_id") or "").strip()
        target = self._target_video(video_id)
        capture_seconds = int(payload.get("capture_seconds", 120))
        profile = (
            Path(str(payload["browser_profile_dir"]))
            if payload.get("browser_profile_dir")
            else self.settings.browser_profile_dir
        )
        result = self.collector(
            video_id=video_id,
            video_url=str(payload.get("video_url") or target["video_url"]),
            video_title=str(payload.get("video_title") or target.get("title", "")),
            batches_dir=self.settings.batches_dir,
            browser_profile_dir=profile,
            capture_seconds=capture_seconds,
        )
        job_result: dict[str, Any] = {
            "capture": dict(result.diagnostics),
            "batch": (
                result.batch_path.relative_to(
                    self.settings.data_dir.resolve()
                ).as_posix()
                if result.batch_path
                else None
            ),
        }
        try:
            with archive_lock(self.settings.comments_dir / ".archive.lock"):
                if result.batch_path is not None:
                    exit_code = ingest_comments.main(
                        [
                            str(result.batch_path),
                            "--store",
                            str(self.settings.canonical_comments_path),
                        ]
                    )
                    if exit_code != 0:
                        raise RuntimeError(
                            "captured batch failed canonical ingestion: "
                            f"{result.batch_path.name}"
                        )

                update_collection_metadata(
                    result,
                    targets_path=self.settings.targets_path,
                    progress_path=self.settings.progress_path,
                    canonical_comments_path=self.settings.canonical_comments_path,
                )
                import_stats, verification = self._sync_mysql()
                job_result["import"] = import_stats
                job_result["verification"] = verification
        except Exception as exc:
            if result.is_blocked:
                raise BlockedJob(
                    f"{result.message}; downstream persistence also failed: "
                    f"{type(exc).__name__}: {exc}",
                    job_result,
                ) from exc
            raise
        return JobExecution(
            status="blocked" if result.is_blocked else "succeeded",
            message=result.message,
            result=job_result,
        )

    def _target_video(self, video_id: str) -> Mapping[str, Any]:
        if not video_id:
            raise ValueError("collect_video job requires video_id")
        manifest = json.loads(self.settings.targets_path.read_text(encoding="utf-8-sig"))
        for target in manifest.get("videos", []):
            if isinstance(target, Mapping) and target.get("video_id") == video_id:
                return target
        raise ValueError(f"video {video_id} is not in the target manifest")


def _configure_scheduler(scheduler: Any, worker: JobWorker, poll_seconds: int) -> Any:
    if poll_seconds < 1:
        raise ValueError("poll_seconds must be positive")
    scheduler.add_job(
        worker.run_once,
        trigger="interval",
        seconds=poll_seconds,
        id="claim-due-collection-job",
        name="Claim one due queued collection job",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=max(10, poll_seconds * 2),
        next_run_time=datetime.now(timezone.utc),
        replace_existing=True,
    )
    return scheduler


def build_scheduler(worker: JobWorker, *, poll_seconds: int = 5) -> Any:
    """Build the foreground scheduler used by ``scripts/run_worker.py``."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError as exc:  # pragma: no cover - dependency setup failure
        raise RuntimeError("APScheduler 3.x is required to run the worker") from exc
    return _configure_scheduler(
        BlockingScheduler(timezone=timezone.utc), worker, poll_seconds
    )


def build_background_scheduler(
    worker: JobWorker, *, poll_seconds: int = 5
) -> Any:
    """Build the non-blocking scheduler for a FastAPI lifespan."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError as exc:  # pragma: no cover - dependency setup failure
        raise RuntimeError("APScheduler 3.x is required to run the worker") from exc
    return _configure_scheduler(
        BackgroundScheduler(timezone=timezone.utc), worker, poll_seconds
    )
