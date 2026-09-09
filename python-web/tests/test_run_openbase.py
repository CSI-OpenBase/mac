from __future__ import annotations

import threading

import pytest

from scripts import run_openbase


def test_configured_parent_pid_accepts_positive_external_pid(monkeypatch) -> None:
    monkeypatch.setattr(run_openbase.os, "name", "posix")
    monkeypatch.setattr(run_openbase.os, "getppid", lambda: 4242)
    monkeypatch.setenv("CSI_OPENBASE_DESKTOP_TOKEN", "desktop-token")
    monkeypatch.setenv("CSI_OPENBASE_PARENT_PID", "4242")

    assert run_openbase._configured_parent_pid() == 4242


def test_configured_parent_pid_rejects_invalid_or_detached_parent(monkeypatch) -> None:
    monkeypatch.setattr(run_openbase.os, "name", "posix")
    monkeypatch.setattr(run_openbase.os, "getppid", lambda: 4242)
    monkeypatch.setenv("CSI_OPENBASE_DESKTOP_TOKEN", "desktop-token")
    monkeypatch.setenv("CSI_OPENBASE_PARENT_PID", "not-a-pid")
    with pytest.raises(RuntimeError, match="must be an integer"):
        run_openbase._configured_parent_pid()

    monkeypatch.setenv("CSI_OPENBASE_PARENT_PID", "111")
    with pytest.raises(RuntimeError, match="orphaned desktop backend"):
        run_openbase._configured_parent_pid()


def test_configured_parent_pid_is_disabled_outside_posix(monkeypatch) -> None:
    monkeypatch.setattr(run_openbase.os, "name", "nt")
    monkeypatch.setenv("CSI_OPENBASE_PARENT_PID", "4242")

    assert run_openbase._configured_parent_pid() is None


def test_configured_parent_pid_requires_desktop_token(monkeypatch) -> None:
    monkeypatch.setattr(run_openbase.os, "name", "posix")
    monkeypatch.setattr(run_openbase.os, "getppid", lambda: 4242)
    monkeypatch.setenv("CSI_OPENBASE_PARENT_PID", "4242")
    monkeypatch.delenv("CSI_OPENBASE_DESKTOP_TOKEN", raising=False)

    assert run_openbase._configured_parent_pid() is None


def test_parent_watchdog_requests_shutdown_after_parent_exits() -> None:
    stopped = threading.Event()
    parents = iter((4242, 1))

    thread = run_openbase._start_parent_watchdog(
        4242,
        stopped.set,
        current_parent_pid=lambda: next(parents),
        interval_seconds=0.001,
    )

    assert stopped.wait(timeout=1)
    thread.join(timeout=1)
    assert not thread.is_alive()
