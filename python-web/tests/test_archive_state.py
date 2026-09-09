from __future__ import annotations

import json
from pathlib import Path

import pytest

import admin_app.archive_lock as archive_module
from admin_app.archive_lock import archive_lock, write_collection_state


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_collection_state_transaction_recovers_interrupted_second_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets_path = tmp_path / "collection-targets.json"
    progress_path = tmp_path / "collection-progress.json"
    targets_path.write_text('{"version": "old"}\n', encoding="utf-8")
    progress_path.write_text('{"version": "old"}\n', encoding="utf-8")
    real_write = archive_module._atomic_write_json
    failed = False

    def interrupted_write(path: Path, value: dict[str, object]) -> None:
        nonlocal failed
        if path == progress_path and not failed:
            failed = True
            raise OSError("simulated interruption")
        real_write(path, value)

    monkeypatch.setattr(archive_module, "_atomic_write_json", interrupted_write)
    with pytest.raises(OSError, match="simulated interruption"):
        write_collection_state(
            targets_path,
            {"version": "new", "status": "complete"},
            progress_path,
            {"version": "new", "status": "complete"},
        )

    assert _read_json(targets_path)["version"] == "new"
    assert _read_json(progress_path)["version"] == "old"
    assert (tmp_path / ".collection-state.transaction.json").exists()

    monkeypatch.setattr(archive_module, "_atomic_write_json", real_write)
    with archive_lock(tmp_path / ".archive.lock"):
        pass

    assert _read_json(targets_path) == {
        "version": "new",
        "status": "complete",
    }
    assert _read_json(progress_path) == {
        "version": "new",
        "status": "complete",
    }
    assert not (tmp_path / ".collection-state.transaction.json").exists()
