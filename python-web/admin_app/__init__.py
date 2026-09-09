"""CSI OpenBase local-first creator data and baseline analysis application."""

from __future__ import annotations

import re
from pathlib import Path


__all__ = ["__version__"]


_VERSION_FILE_PATTERN = re.compile(
    r"(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.[1-9][0-9])"
    r"(?:\r?\n)?"
)


def _read_version() -> str:
    candidates = (
        Path(__file__).with_name("VERSION"),
        Path(__file__).resolve().parents[1] / "VERSION",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw_version = path.read_text(encoding="ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"invalid CSI OpenBase VERSION file: {path}") from exc
        match = _VERSION_FILE_PATTERN.fullmatch(raw_version)
        if match is None:
            raise RuntimeError(f"invalid CSI OpenBase VERSION file: {path}")
        return match.group("version")
    raise RuntimeError("CSI OpenBase VERSION file is missing")


__version__ = _read_version()
