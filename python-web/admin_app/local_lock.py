"""Non-blocking process lease for one local archive directory."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO


_REGISTRY_LOCK = threading.Lock()
_PROCESS_LEASES: set[str] = set()


class WorkspaceInUseError(RuntimeError):
    pass


class WorkspaceLease:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._handle: BinaryIO | None = None
        self._key = os.path.normcase(str(self.path))

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("workspace lease is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _REGISTRY_LOCK:
            if self._key in _PROCESS_LEASES:
                raise WorkspaceInUseError("该工作目录已由另一个 CSI OpenBase 实例使用")
            handle = self.path.open("a+b")
            try:
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on non-Windows CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                raise WorkspaceInUseError(
                    "该工作目录已由另一个 CSI OpenBase 实例使用"
                ) from exc
            _PROCESS_LEASES.add(self._key)
            self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            with _REGISTRY_LOCK:
                _PROCESS_LEASES.discard(self._key)

    def __enter__(self) -> WorkspaceLease:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
