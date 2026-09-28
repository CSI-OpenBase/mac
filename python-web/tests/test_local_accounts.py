from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import admin_app.local_jobs as local_jobs_module
from admin_app.local_accounts import (
    LocalAccountError,
    LocalAccountManager,
    LocalAccountRegistry,
    PRIMARY_ACCOUNT_ID,
)
from admin_app.local_app import create_local_app
from admin_app.local_cleanup import clear_local_data
from admin_app.local_config import LocalSettings


VIDEO_ID = "7654321098765432109"


class FakeBackgroundRunner:
    def __init__(self, store: Any, settings: LocalSettings) -> None:
        self.store = store
        self.settings = settings
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> bool:
        self.closed = True
        self.store.interrupt_active_jobs()
        return True

    def submit(self, kind: str, **payload: Any) -> dict[str, Any]:
        return self.store.create_job(
            kind,
            video_id=payload.get("video_id"),
            payload=payload,
        )


def local_settings(tmp_path: Path) -> LocalSettings:
    return LocalSettings(
        data_home=tmp_path / "openbase",
        session_home=tmp_path / "sessions",
    )


def csrf(client: TestClient, path: str = "/") -> str:
    response = client.get(path)
    marker = 'name="csrf_token" value="'
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def video_record(video_id: str = VIDEO_ID) -> dict[str, Any]:
    return {
        "video_id": video_id,
        "title": "账号隔离测试视频",
        "video_url": f"https://www.douyin.com/video/{video_id}",
        "manifest_path": f"works/videos/douyin/{video_id}/manifest.json",
        "first_seen_at": "2026-09-28T00:00:00Z",
        "last_seen_at": "2026-09-28T00:00:00Z",
    }


def test_registry_adopts_existing_workspace_without_moving_data(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    existing = settings.data_home / "works" / "keep.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("preserve", encoding="utf-8")

    registry = LocalAccountRegistry(settings)
    registry.load()

    primary = registry.get(PRIMARY_ACCOUNT_ID)
    assert primary.data_directory == "."
    assert primary.session_directory == "."
    assert registry.active_account_id == PRIMARY_ACCOUNT_ID
    assert existing.read_text(encoding="utf-8") == "preserve"
    payload = json.loads(registry.path.read_text(encoding="utf-8"))
    assert payload["active_account_id"] == PRIMARY_ACCOUNT_ID
    assert payload["accounts"][0]["data_directory"] == "."


def test_registry_rejects_tampered_account_directory(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    settings.data_home.mkdir(parents=True)
    (settings.data_home / ".openbase-accounts.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_account_id": "unsafe",
                "accounts": [
                    {
                        "account_id": "unsafe",
                        "name": "越界账号",
                        "data_directory": "../outside",
                        "session_directory": "accounts/unsafe",
                        "created_at": "2026-09-28T00:00:00Z",
                        "updated_at": "2026-09-28T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalAccountError, match="越界目录"):
        LocalAccountRegistry(settings).load()
    assert not (tmp_path / "outside").exists()


def test_registry_refuses_unowned_existing_accounts_directory(
    tmp_path: Path,
) -> None:
    settings = local_settings(tmp_path)
    registry = LocalAccountRegistry(settings)
    registry.load()
    sentinel = settings.data_home / "accounts" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("user-owned", encoding="utf-8")

    with pytest.raises(LocalAccountError, match="非 OpenBase"):
        registry.create("第二账号")

    assert sentinel.read_text(encoding="utf-8") == "user-owned"
    assert registry.list(include_archived=True) == [registry.get(PRIMARY_ACCOUNT_ID)]


def test_manager_isolates_account_data_sessions_and_identity(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    manager = LocalAccountManager(
        settings,
        runner_factory=FakeBackgroundRunner,
    )
    manager.start()
    try:
        primary = manager.current
        primary.store.set_meta(
            "creator_identity",
            {"handle": "creator-one", "display_name": "创作者一"},
        )
        primary.store.upsert_videos([video_record()])

        secondary = manager.create_and_switch("第二账号")
        secondary_id = secondary.account.account_id
        assert secondary.settings.data_home == (
            settings.data_home / "accounts" / secondary_id
        ).resolve()
        assert secondary.settings.session_home == (
            settings.session_home / "accounts" / secondary_id
        ).resolve()
        assert secondary.store.get_meta("creator_identity", {}) == {}
        assert secondary.store.video_count() == 0
        secondary.store.set_meta(
            "creator_identity",
            {"handle": "creator-two", "display_name": "创作者二"},
        )

        restored_primary = manager.switch(PRIMARY_ACCOUNT_ID)
        assert restored_primary.settings.data_home == settings.data_home.resolve()
        assert restored_primary.settings.session_home == settings.session_home.resolve()
        assert restored_primary.store.get_meta("creator_identity", {})["handle"] == (
            "creator-one"
        )
        assert restored_primary.store.video_count() == 1

        summaries = {
            item["account_id"]: item for item in manager.account_summaries()
        }
        assert summaries[PRIMARY_ACCOUNT_ID]["video_count"] == 1
        assert summaries[secondary_id]["identity"]["handle"] == "creator-two"
    finally:
        assert manager.close() is True


def test_manager_refuses_switch_while_current_account_has_active_job(
    tmp_path: Path,
) -> None:
    manager = LocalAccountManager(
        local_settings(tmp_path),
        runner_factory=FakeBackgroundRunner,
    )
    manager.start()
    try:
        secondary = manager.registry.create("第二账号")
        manager.current.store.create_job("authorize")

        with pytest.raises(LocalAccountError, match="任务等待或运行"):
            manager.switch(secondary.account_id)
        assert manager.current.account.account_id == PRIMARY_ACCOUNT_ID
    finally:
        assert manager.close() is True


def test_manager_restores_previous_account_when_target_cannot_start(
    tmp_path: Path,
) -> None:
    settings = local_settings(tmp_path)

    def runner_factory(store: Any, account_settings: LocalSettings) -> Any:
        if account_settings.data_home != settings.data_home.resolve():
            raise RuntimeError("target runner failed")
        return FakeBackgroundRunner(store, account_settings)

    manager = LocalAccountManager(settings, runner_factory=runner_factory)
    manager.start()
    try:
        secondary = manager.registry.create("无法启动的账号")

        with pytest.raises(RuntimeError, match="target runner failed"):
            manager.switch(secondary.account_id)
        assert manager.current.account.account_id == PRIMARY_ACCOUNT_ID
        assert manager.registry.active_account_id == PRIMARY_ACCOUNT_ID
        assert manager.current.settings.data_home == settings.data_home.resolve()
    finally:
        assert manager.close() is True


def test_clear_data_never_enters_sibling_account_directory(tmp_path: Path) -> None:
    settings = local_settings(tmp_path)
    manager = LocalAccountManager(settings, runner_factory=FakeBackgroundRunner)
    manager.start()
    try:
        secondary = manager.create_and_switch("第二账号")
        sibling_file = secondary.settings.works_dir / "sibling.txt"
        sibling_file.parent.mkdir(parents=True, exist_ok=True)
        sibling_file.write_text("preserve", encoding="utf-8")

        primary = manager.switch(PRIMARY_ACCOUNT_ID)
        primary_file = primary.settings.works_dir / "primary.txt"
        primary_file.parent.mkdir(parents=True, exist_ok=True)
        primary_file.write_text("clear", encoding="utf-8")
        clear_local_data(primary.settings, primary.store, "all")

        assert not primary_file.exists()
        assert sibling_file.read_text(encoding="utf-8") == "preserve"
        assert (settings.data_home / ".openbase-accounts.json").is_file()
    finally:
        assert manager.close() is True


def test_account_management_routes_switch_and_soft_remove_without_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_jobs_module, "LocalJobRunner", FakeBackgroundRunner)
    app = create_local_app(local_settings(tmp_path))

    with TestClient(app) as client:
        manager: LocalAccountManager = app.state.local_accounts
        manager.current.store.set_meta(
            "creator_identity",
            {"handle": "creator-one", "display_name": "创作者一"},
        )
        manager.current.store.upsert_videos([video_record()])

        home = client.get("/")
        assert home.status_code == 200
        assert 'href="/accounts"' in home.text
        assert "默认账号" in home.text
        token = csrf(client)
        created = client.post(
            "/accounts",
            data={"csrf_token": token, "name": "品牌副账号"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        secondary_id = manager.current.account.account_id
        secondary_home = manager.current.settings.data_home
        marker = secondary_home / "preserve.txt"
        marker.write_text("keep", encoding="utf-8")
        assert manager.current.store.get_meta("creator_identity", {}) == {}
        assert manager.current.store.video_count() == 0

        manager.current.store.set_meta(
            "creator_identity",
            {"handle": "creator-two", "display_name": "创作者二"},
        )
        token = csrf(client, "/accounts")
        switched = client.post(
            f"/accounts/{PRIMARY_ACCOUNT_ID}/switch",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert switched.status_code == 303
        assert manager.current.account.account_id == PRIMARY_ACCOUNT_ID
        assert manager.current.store.video_count() == 1

        token = csrf(client, "/accounts")
        archived = client.post(
            f"/accounts/{secondary_id}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert archived.status_code == 303
        assert manager.registry.get(secondary_id).archived is True
        assert marker.read_text(encoding="utf-8") == "keep"

        token = csrf(client, "/accounts")
        restored = client.post(
            f"/accounts/{secondary_id}/restore",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert restored.status_code == 303
        accounts_page = client.get("/accounts")
        assert "品牌副账号" in accounts_page.text
        assert "创作者二" in accounts_page.text
        assert "移除账号只隐藏入口" in accounts_page.text
