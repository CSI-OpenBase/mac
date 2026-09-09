#!/usr/bin/env python3
"""Start the local-only CSI OpenBase creator data dashboard."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import uvicorn  # noqa: E402

from admin_app.config import load_settings  # noqa: E402
from admin_app.main import create_app  # noqa: E402


def main() -> None:
    settings = load_settings()
    if settings.web_host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The dashboard may only bind to a loopback address")
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
