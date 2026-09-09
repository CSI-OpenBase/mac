#!/usr/bin/env python3
"""Run the file-first CSI OpenBase local application."""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

def _configure_frozen_runtime() -> None:
    """Point Playwright at the Chromium directory shipped by PyInstaller."""

    if not getattr(sys, "frozen", False):
        return
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    browser_root = bundle_root / "ms-playwright"
    if browser_root.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)


_configure_frozen_runtime()


from admin_app import __version__  # noqa: E402
from admin_app.local_app import create_local_app  # noqa: E402
from admin_app.local_config import load_local_settings  # noqa: E402


def _configured_parent_pid() -> int | None:
    # Windows desktop builds use a Job Object; PID polling is for POSIX hosts.
    if os.name != "posix" or not os.environ.get("CSI_OPENBASE_DESKTOP_TOKEN"):
        return None
    raw_pid = os.environ.get("CSI_OPENBASE_PARENT_PID", "").strip()
    if not raw_pid:
        return None
    try:
        parent_pid = int(raw_pid)
    except ValueError as exc:
        raise RuntimeError("CSI_OPENBASE_PARENT_PID must be an integer") from exc
    if parent_pid <= 1 or parent_pid != os.getppid():
        raise RuntimeError(
            "CSI_OPENBASE_PARENT_PID is no longer the direct parent; "
            "refusing to start an orphaned desktop backend"
        )
    return parent_pid


def _start_parent_watchdog(
    parent_pid: int,
    request_shutdown: Callable[[], None],
    *,
    current_parent_pid: Callable[[], int] = os.getppid,
    interval_seconds: float = 1.0,
) -> threading.Thread:
    interval = threading.Event()

    def watch() -> None:
        while current_parent_pid() == parent_pid:
            interval.wait(interval_seconds)
        logging.getLogger(__name__).warning(
            "desktop parent process %s exited; stopping local backend", parent_pid
        )
        request_shutdown()

    thread = threading.Thread(
        target=watch,
        name="openbase-parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


def main() -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required to run CSI OpenBase") from exc
    settings = load_local_settings()
    log_path = settings.log_dir / "openbase.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger(__name__).info(
        "CSI OpenBase %s; data home: %s", __version__, settings.data_home
    )
    server = None

    def request_shutdown() -> None:
        if server is not None:
            server.should_exit = True

    app = create_local_app(settings, shutdown_callback=request_shutdown)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    parent_pid = _configured_parent_pid()
    if parent_pid is not None:
        _start_parent_watchdog(parent_pid, request_shutdown)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
