from __future__ import annotations

from pathlib import Path

import pytest

from admin_app.local_lock import WorkspaceInUseError, WorkspaceLease


def test_workspace_lease_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / ".openbase.instance.lock"
    first = WorkspaceLease(path)
    second = WorkspaceLease(path)

    first.acquire()
    try:
        with pytest.raises(WorkspaceInUseError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
