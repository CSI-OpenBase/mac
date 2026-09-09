#!/usr/bin/env python3
"""Calculate or apply the next CSI OpenBase release version."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPOSITORY_ROOT / "VERSION"
VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>[1-9][0-9])$"
)


def validate_version(value: str) -> re.Match[str]:
    match = VERSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("version must use x.x.xx with a patch from 10 through 99")
    return match


def read_version(path: Path = VERSION_FILE) -> str:
    try:
        raw_version = path.read_text(encoding="ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("VERSION must contain one ASCII version line") from exc
    match = re.fullmatch(
        r"(?P<version>(?:0|[1-9][0-9]*)\."
        r"(?:0|[1-9][0-9]*)\."
        r"[1-9][0-9])(?:\r?\n)?",
        raw_version,
    )
    if match is None:
        raise ValueError("VERSION must contain exactly one x.x.xx version line")
    return match.group("version")


def next_version(current: str) -> str:
    match = validate_version(current)
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    if patch == 99:
        minor += 1
        patch = 10
    else:
        patch += 1
    return f"{major}.{minor}.{patch:02d}"


def write_version(version: str, path: Path = VERSION_FILE) -> None:
    validate_version(version)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="ascii",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(f"{version}\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-version")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the calculated version to the repository VERSION file",
    )
    args = parser.parse_args()
    if args.apply and args.current_version is not None:
        parser.error("--apply always reads the repository VERSION file")

    current = args.current_version or read_version()
    calculated = next_version(current)
    if args.apply:
        write_version(calculated)
    print(calculated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
