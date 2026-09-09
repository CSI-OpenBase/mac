from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from admin_app import __version__
from scripts.bump_version import next_version, read_version, write_version


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(
    rb"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.[1-9][0-9](?:\r?\n)?"
)


def test_root_version_is_the_runtime_and_package_source() -> None:
    raw_version = (ROOT / "VERSION").read_bytes()
    assert VERSION_PATTERN.fullmatch(raw_version)
    version = raw_version.decode("ascii").strip()
    assert __version__ == version

    configuration = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "version" not in configuration["project"]
    assert configuration["project"]["dynamic"] == ["version"]
    assert configuration["tool"]["hatch"]["version"]["path"] == "VERSION"
    force_include = configuration["tool"]["hatch"]["build"]["targets"][
        "wheel"
    ]["force-include"]
    assert force_include["VERSION"] == "admin_app/VERSION"


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("0.0.10", "0.0.11"),
        ("0.0.98", "0.0.99"),
        ("0.0.99", "0.1.10"),
        ("1.1.99", "1.2.10"),
    ],
)
def test_next_version_follows_release_contract(current: str, expected: str) -> None:
    assert next_version(current) == expected


@pytest.mark.parametrize("invalid", ["0.0.0", "0.0.9", "0.0.100", "01.0.10"])
def test_next_version_rejects_invalid_versions(invalid: str) -> None:
    with pytest.raises(ValueError, match="x.x.xx"):
        next_version(invalid)


def test_version_file_writer_uses_one_ascii_line(tmp_path: Path) -> None:
    path = tmp_path / "VERSION"
    write_version("2.3.10", path)

    assert path.read_bytes() == b"2.3.10\n"
    assert read_version(path) == "2.3.10"


def test_version_reader_accepts_a_windows_line_ending(tmp_path: Path) -> None:
    path = tmp_path / "VERSION"
    path.write_bytes(b"2.3.10\r\n")

    assert read_version(path) == "2.3.10"


@pytest.mark.parametrize(
    "raw_version",
    [b" 2.3.10\n", b"2.3.10 \n", b"2.3.10\n\n", b"\xff\n"],
)
def test_version_reader_rejects_extra_content(
    tmp_path: Path, raw_version: bytes
) -> None:
    path = tmp_path / "VERSION"
    path.write_bytes(raw_version)

    with pytest.raises(ValueError, match="VERSION"):
        read_version(path)
