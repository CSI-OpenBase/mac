#!/usr/bin/env python3
"""Run the active workspace's single local collection worker."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from admin_app.archive_lock import archive_lock  # noqa: E402
from admin_app.config import load_settings  # noqa: E402
from admin_app.database import initialize_database_engine  # noqa: E402
from admin_app.jobs import (  # noqa: E402
    JobWorker,
    WorkerAlreadyRunning,
    WorkerInstanceLock,
    build_scheduler,
)
from admin_app.repository import Repository  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim at most one due job and exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        help="queue poll interval (default: CSI OpenBase worker setting, normally 5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(prompt_for_password=True)
    poll_seconds = args.poll_seconds or settings.job_poll_seconds
    if poll_seconds < 1:
        print("poll interval must be positive", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    lock_path = (
        settings.browser_profile_dir.parent
        / f"{settings.workspace_slug}.collection-worker.lock"
    )
    engine = None
    try:
        with WorkerInstanceLock(lock_path):
            with archive_lock(settings.comments_dir / ".archive.lock"):
                pass
            engine = initialize_database_engine(settings)
            repository = Repository(engine)
            recovered = repository.requeue_stale_jobs(stale_after=None)
            if recovered:
                logging.getLogger(__name__).warning(
                    "recovered %s orphaned job(s) from the previous worker", recovered
                )
            worker = JobWorker(repository, settings)
            if args.once:
                worker.run_once()
                return 0
            scheduler = build_scheduler(worker, poll_seconds=poll_seconds)
            logging.getLogger(__name__).info(
                "worker %s started; polling every %s seconds",
                worker.worker_id,
                poll_seconds,
            )
            try:
                scheduler.start()
            except (KeyboardInterrupt, SystemExit):
                logging.getLogger(__name__).info("worker stopped")
            return 0
    except WorkerAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 4
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
