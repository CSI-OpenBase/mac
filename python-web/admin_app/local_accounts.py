"""Local multi-account workspaces with isolated data and browser sessions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .local_cleanup import recover_local_cleanup
from .local_config import LocalSettings
from .local_lock import WorkspaceLease
from .local_store import LocalStore, utc_now


ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_ACCOUNT_NAME_LENGTH = 80
ACCOUNT_REGISTRY_FILENAME = ".openbase-accounts.json"
ACCOUNT_CONTAINER_NAME = "accounts"
ACCOUNT_CONTAINER_MARKER = ".csi-openbase-owned"
PRIMARY_ACCOUNT_ID = "primary"


class LocalAccountError(RuntimeError):
    """Raised when an account-management operation cannot be completed safely."""


def _account_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise ValueError("账号名称不能为空")
    if len(name) > MAX_ACCOUNT_NAME_LENGTH:
        raise ValueError(f"账号名称不能超过 {MAX_ACCOUNT_NAME_LENGTH} 个字符")
    return name


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2)
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


@dataclass(frozen=True, slots=True)
class LocalAccount:
    account_id: str
    name: str
    data_directory: str
    session_directory: str
    created_at: str
    updated_at: str
    archived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "data_directory": self.data_directory,
            "session_directory": self.session_directory,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived": self.archived,
        }


@dataclass(slots=True)
class LocalAccountRuntime:
    account: LocalAccount
    settings: LocalSettings
    store: LocalStore
    runner: Any
    lease: WorkspaceLease
    owns_runner: bool


class LocalAccountRegistry:
    """Durable account list rooted in the user-selected OpenBase directory."""

    def __init__(self, root_settings: LocalSettings) -> None:
        self.root_settings = root_settings
        self.path = root_settings.data_home / ACCOUNT_REGISTRY_FILENAME
        self._accounts: dict[str, LocalAccount] = {}
        self._active_account_id = PRIMARY_ACCOUNT_ID

    @property
    def active_account_id(self) -> str:
        return self._active_account_id

    def load(self) -> None:
        if not self.path.exists():
            now = utc_now()
            primary = LocalAccount(
                account_id=PRIMARY_ACCOUNT_ID,
                name="默认账号",
                data_directory=".",
                session_directory=".",
                created_at=now,
                updated_at=now,
            )
            self._accounts = {primary.account_id: primary}
            self._active_account_id = primary.account_id
            self._save()
            return
        if not self.path.is_file() or self.path.is_symlink():
            raise LocalAccountError("账号注册表不是安全的普通文件")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalAccountError("账号注册表无法读取或格式损坏") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise LocalAccountError("账号注册表版本无效")
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise LocalAccountError("账号注册表中没有可用账号")
        accounts: dict[str, LocalAccount] = {}
        for raw in raw_accounts:
            account = self._parse_account(raw)
            if account.account_id in accounts:
                raise LocalAccountError("账号注册表包含重复账号")
            accounts[account.account_id] = account
        active_account_id = str(payload.get("active_account_id") or "")
        active = accounts.get(active_account_id)
        if active is None or active.archived:
            raise LocalAccountError("账号注册表中的当前账号无效")
        self._accounts = accounts
        self._active_account_id = active_account_id

    def load_ephemeral(self) -> None:
        now = utc_now()
        primary = LocalAccount(
            account_id=PRIMARY_ACCOUNT_ID,
            name="默认账号",
            data_directory=".",
            session_directory=".",
            created_at=now,
            updated_at=now,
        )
        self._accounts = {primary.account_id: primary}
        self._active_account_id = primary.account_id

    def _parse_account(self, raw: Any) -> LocalAccount:
        if not isinstance(raw, dict):
            raise LocalAccountError("账号注册表包含无效记录")
        account_id = str(raw.get("account_id") or "")
        if not ACCOUNT_ID_RE.fullmatch(account_id):
            raise LocalAccountError("账号注册表包含无效账号 ID")
        expected_directory = (
            "." if account_id == PRIMARY_ACCOUNT_ID else f"accounts/{account_id}"
        )
        data_directory = str(raw.get("data_directory") or "")
        session_directory = str(raw.get("session_directory") or "")
        if data_directory != expected_directory or session_directory != expected_directory:
            raise LocalAccountError("账号注册表包含越界目录")
        try:
            name = _account_name(raw.get("name"))
        except ValueError as exc:
            raise LocalAccountError(str(exc)) from exc
        created_at = str(raw.get("created_at") or "")
        updated_at = str(raw.get("updated_at") or "")
        if not created_at or not updated_at:
            raise LocalAccountError("账号注册表缺少时间信息")
        return LocalAccount(
            account_id=account_id,
            name=name,
            data_directory=data_directory,
            session_directory=session_directory,
            created_at=created_at,
            updated_at=updated_at,
            archived=bool(raw.get("archived", False)),
        )

    def _save(self) -> None:
        _atomic_json(
            self.path,
            {
                "schema_version": 1,
                "active_account_id": self._active_account_id,
                "accounts": [
                    account.as_dict()
                    for account in sorted(
                        self._accounts.values(), key=lambda item: item.created_at
                    )
                ],
            },
        )

    def get(self, account_id: str) -> LocalAccount:
        account = self._accounts.get(account_id)
        if account is None:
            raise KeyError("账号不存在")
        return account

    def list(self, *, include_archived: bool = True) -> list[LocalAccount]:
        return [
            account
            for account in sorted(
                self._accounts.values(), key=lambda item: item.created_at
            )
            if include_archived or not account.archived
        ]

    def create(self, name: str) -> LocalAccount:
        self._prepare_account_container(self.root_settings.data_home)
        self._prepare_account_container(self.root_settings.session_home)
        account_id = uuid.uuid4().hex[:16]
        now = utc_now()
        account = LocalAccount(
            account_id=account_id,
            name=_account_name(name),
            data_directory=f"accounts/{account_id}",
            session_directory=f"accounts/{account_id}",
            created_at=now,
            updated_at=now,
        )
        self._accounts[account_id] = account
        self._save()
        return account

    @staticmethod
    def _prepare_account_container(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        container = root / ACCOUNT_CONTAINER_NAME
        marker = container / ACCOUNT_CONTAINER_MARKER
        if container.exists():
            is_junction = getattr(container, "is_junction", None)
            if (
                not container.is_dir()
                or container.is_symlink()
                or bool(is_junction and is_junction())
            ):
                raise LocalAccountError("账号目录不是安全的普通目录")
            if not marker.is_file():
                try:
                    next(container.iterdir())
                except StopIteration:
                    marker.write_text("CSI OpenBase account workspaces\n", encoding="utf-8")
                else:
                    raise LocalAccountError(
                        "工作目录中已有非 OpenBase 的 accounts 目录，未写入账号数据"
                    )
        else:
            container.mkdir()
            marker.write_text("CSI OpenBase account workspaces\n", encoding="utf-8")
        if marker.is_symlink() or not marker.is_file():
            raise LocalAccountError("账号目录缺少有效的程序所有权标记")

    def rename(self, account_id: str, name: str) -> LocalAccount:
        account = self.get(account_id)
        updated = replace(account, name=_account_name(name), updated_at=utc_now())
        self._accounts[account_id] = updated
        self._save()
        return updated

    def activate(self, account_id: str) -> LocalAccount:
        account = self.get(account_id)
        if account.archived:
            raise LocalAccountError("已移除账号不能切换，请先恢复")
        self._active_account_id = account_id
        self._save()
        return account

    def archive(self, account_id: str) -> LocalAccount:
        account = self.get(account_id)
        if account.account_id == PRIMARY_ACCOUNT_ID:
            raise LocalAccountError("默认账号不能移除")
        if account.account_id == self._active_account_id:
            raise LocalAccountError("当前账号不能移除，请先切换账号")
        updated = replace(account, archived=True, updated_at=utc_now())
        self._accounts[account_id] = updated
        self._save()
        return updated

    def restore(self, account_id: str) -> LocalAccount:
        account = self.get(account_id)
        updated = replace(account, archived=False, updated_at=utc_now())
        self._accounts[account_id] = updated
        self._save()
        return updated


class LocalAccountManager:
    """Own the active account runtime and perform atomic, idle-only switches."""

    def __init__(
        self,
        root_settings: LocalSettings,
        *,
        store: LocalStore | None = None,
        runner: Any | None = None,
        runner_factory: Callable[[LocalStore, LocalSettings], Any] | None = None,
    ) -> None:
        if (store is None) != (runner is None):
            raise ValueError("store and runner must be supplied together")
        self.root_settings = root_settings
        self.registry = LocalAccountRegistry(root_settings)
        self._injected_store = store
        self._injected_runner = runner
        self._runner_factory = runner_factory or self._default_runner
        self._managed = store is None
        self._runtime: LocalAccountRuntime | None = None
        self._manager_lease = WorkspaceLease(
            root_settings.data_home / ".openbase.accounts.lock"
        )
        self._lock = threading.RLock()
        self._started = False
        self._shutdown_result: bool | None = None

    @staticmethod
    def _default_runner(store: LocalStore, settings: LocalSettings) -> Any:
        from .local_jobs import LocalJobRunner

        return LocalJobRunner(store, settings)

    @property
    def managed(self) -> bool:
        return self._managed

    @property
    def current(self) -> LocalAccountRuntime:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("local account manager is not started")
        return runtime

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.root_settings.ensure_directories()
            if self._managed:
                self._manager_lease.acquire()
            try:
                if self._managed:
                    self.registry.load()
                else:
                    self.registry.load_ephemeral()
                account = self.registry.get(self.registry.active_account_id)
                self._runtime = self._open_runtime(account)
                self._started = True
            except Exception:
                if self._managed:
                    self._manager_lease.release()
                raise

    def close(self) -> bool:
        with self._lock:
            if self._shutdown_result is not None:
                return self._shutdown_result
            runtime = self._runtime
            stopped = True
            if runtime is not None:
                stopped = self._close_runtime(runtime)
            if stopped:
                self._runtime = None
                if self._managed:
                    self._manager_lease.release()
            self._shutdown_result = stopped
            return stopped

    def _settings_for(self, account: LocalAccount) -> LocalSettings:
        data_home = self._account_path(
            self.root_settings.data_home, account.data_directory, "账号数据目录"
        )
        session_home = self._account_path(
            self.root_settings.session_home,
            account.session_directory,
            "账号浏览器目录",
        )
        return replace(
            self.root_settings,
            data_home=data_home,
            session_home=session_home,
        )

    @staticmethod
    def _account_path(root: Path, relative: str, label: str) -> Path:
        candidate = root if relative == "." else root / Path(relative)
        resolved_root = root.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            raise LocalAccountError(f"{label}越过工作目录边界")
        if candidate.exists():
            is_junction = getattr(candidate, "is_junction", None)
            if candidate.is_symlink() or bool(is_junction and is_junction()):
                raise LocalAccountError(f"{label}不能使用链接或目录联接点")
        return resolved

    def _open_runtime(self, account: LocalAccount) -> LocalAccountRuntime:
        settings = self._settings_for(account)
        settings.ensure_directories()
        lease = WorkspaceLease(settings.data_home / ".openbase.instance.lock")
        lease.acquire()
        try:
            store = (
                self._injected_store
                if not self._managed
                else LocalStore(settings.database_path)
            )
            assert store is not None
            store.interrupt_active_jobs()
            recover_local_cleanup(settings, store)
            runner = (
                self._injected_runner
                if not self._managed
                else self._runner_factory(store, settings)
            )
            assert runner is not None
            if hasattr(runner, "start"):
                runner.start()
            return LocalAccountRuntime(
                account=account,
                settings=settings,
                store=store,
                runner=runner,
                lease=lease,
                owns_runner=self._managed,
            )
        except Exception:
            lease.release()
            raise

    @staticmethod
    def _close_runtime(runtime: LocalAccountRuntime) -> bool:
        stopped = True
        if runtime.owns_runner and hasattr(runtime.runner, "close"):
            stopped = runtime.runner.close() is not False
        if stopped:
            runtime.lease.release()
        return stopped

    def switch(self, account_id: str) -> LocalAccountRuntime:
        with self._lock:
            current = self.current
            if account_id == current.account.account_id:
                return current
            target = self.registry.get(account_id)
            if target.archived:
                raise LocalAccountError("已移除账号不能切换，请先恢复")
            if current.store.active_job_count():
                raise LocalAccountError("当前账号仍有任务等待或运行，暂时不能切换")
            old_account = current.account
            if not self._close_runtime(current):
                raise LocalAccountError("当前账号任务线程尚未结束，暂时不能切换")
            try:
                replacement = self._open_runtime(target)
            except Exception:
                self._runtime = self._open_runtime(old_account)
                raise
            try:
                self.registry.activate(account_id)
            except Exception:
                self._close_runtime(replacement)
                self._runtime = self._open_runtime(old_account)
                raise
            self._runtime = replacement
            return replacement

    def create_and_switch(self, name: str) -> LocalAccountRuntime:
        with self._lock:
            if self.current.store.active_job_count():
                raise LocalAccountError("当前账号仍有任务等待或运行，暂时不能添加账号")
            account = self.registry.create(name)
            return self.switch(account.account_id)

    def rename(self, account_id: str, name: str) -> LocalAccount:
        with self._lock:
            account = self.registry.rename(account_id, name)
            if self.current.account.account_id == account_id:
                self.current.account = account
            return account

    def archive(self, account_id: str) -> LocalAccount:
        with self._lock:
            return self.registry.archive(account_id)

    def restore(self, account_id: str) -> LocalAccount:
        with self._lock:
            return self.registry.restore(account_id)

    def account_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            current_id = self.current.account.account_id
            return [
                self._account_summary(account, current_id=current_id)
                for account in self.registry.list(include_archived=True)
            ]

    def current_summary(self) -> dict[str, Any]:
        with self._lock:
            return self._account_summary(
                self.current.account,
                current_id=self.current.account.account_id,
            )

    def _account_summary(
        self, account: LocalAccount, *, current_id: str
    ) -> dict[str, Any]:
        settings = self._settings_for(account)
        if account.account_id == current_id:
            identity = self.current.store.get_meta("creator_identity", {})
            video_count = self.current.store.video_count()
            active_jobs = self.current.store.active_job_count()
        else:
            identity, video_count, active_jobs = _read_account_index(
                settings.database_path
            )
        return {
            **account.as_dict(),
            "active": account.account_id == current_id,
            "identity": identity,
            "authorized": bool(identity and identity.get("handle")),
            "video_count": video_count,
            "active_jobs": active_jobs,
            "data_home": str(settings.data_home),
        }


def _read_account_index(path: Path) -> tuple[dict[str, Any], int, int]:
    if not path.is_file() or path.is_symlink():
        return {}, 0, 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        with connection:
            row = connection.execute(
                "SELECT value_json FROM app_meta WHERE key = 'creator_identity'"
            ).fetchone()
            identity = json.loads(str(row["value_json"])) if row else {}
            video_count = int(
                connection.execute("SELECT COUNT(*) FROM archive_videos").fetchone()[0]
            )
            active_jobs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM archive_jobs "
                    "WHERE status IN ('queued', 'running')"
                ).fetchone()[0]
            )
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return {}, 0, 0
    finally:
        if connection is not None:
            connection.close()
    return identity if isinstance(identity, dict) else {}, video_count, active_jobs


class CurrentRuntimeProxy:
    """Resolve one runtime attribute at access time after an account switch."""

    def __init__(self, manager: LocalAccountManager, attribute: str) -> None:
        self.manager = manager
        self.attribute = attribute

    def __getattr__(self, name: str) -> Any:
        return getattr(getattr(self.manager.current, self.attribute), name)
