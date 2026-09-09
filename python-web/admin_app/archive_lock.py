"""Cross-thread and cross-process lock for the file-backed comment archive."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


_PROCESS_LOCK = threading.RLock()
_THREAD_STATE = threading.local()
_STATE_TRANSACTION_NAME = ".collection-state.transaction.json"


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
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
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _recover_collection_state_transaction(directory: Path) -> None:
    journal = directory / _STATE_TRANSACTION_NAME
    if not journal.exists():
        return
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    if transaction.get("schema_version") != 1:
        raise ValueError(f"Unsupported collection state transaction: {journal}")
    files = transaction.get("files")
    if not isinstance(files, list) or len(files) != 2:
        raise ValueError(f"Invalid collection state transaction: {journal}")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid collection state transaction: {journal}")
        name = item.get("name")
        value = item.get("value")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(value, dict)
        ):
            raise ValueError(f"Invalid collection state transaction: {journal}")
        _atomic_write_json(directory / name, value)
    journal.unlink()


def write_collection_state(
    targets_path: Path,
    targets: Mapping[str, Any],
    progress_path: Path,
    progress: Mapping[str, Any],
) -> None:
    """Replace the two collection-state documents as one recoverable transaction."""
    targets_path = targets_path.resolve()
    progress_path = progress_path.resolve()
    if targets_path.parent != progress_path.parent:
        raise ValueError("collection targets and progress must share a directory")
    directory = targets_path.parent
    transaction = {
        "schema_version": 1,
        "files": [
            {"name": targets_path.name, "value": dict(targets)},
            {"name": progress_path.name, "value": dict(progress)},
        ],
    }
    with archive_lock(directory / ".archive.lock"):
        journal = directory / _STATE_TRANSACTION_NAME
        _atomic_write_json(journal, transaction)
        _atomic_write_json(targets_path, targets)
        _atomic_write_json(progress_path, progress)
        journal.unlink()


@contextmanager
def archive_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-replace operations, including nested calls."""
    resolved = path.resolve()
    key = str(resolved)
    depths = getattr(_THREAD_STATE, "depths", None)
    if depths is None:
        depths = {}
        _THREAD_STATE.depths = depths

    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    resolved.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        handle = resolved.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.1)
            else:  # pragma: no cover - Windows is the project host
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _recover_collection_state_transaction(resolved.parent)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Windows is the project host
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
