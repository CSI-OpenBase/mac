from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import admin_app.local_app as local_app_module
from admin_app import __version__
from admin_app.local_app import create_local_app
from admin_app.local_cleanup import ClearDataResult
from admin_app.local_config import LocalSettings
from admin_app.local_store import LocalStore


class FakeRunner:
    def __init__(self, store: LocalStore) -> None:
        self.store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start(self) -> None:
        return None

    def submit(self, kind: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((kind, payload))
        return self.store.create_job(
            kind, video_id=payload.get("video_id"), payload=payload
        )


def settings(
    tmp_path: Path, token: str = "", instance_nonce: str = ""
) -> LocalSettings:
    return LocalSettings(
        data_home=tmp_path,
        session_home=tmp_path / ".sessions",
        desktop_token=token,
        instance_nonce=instance_nonce,
    )


def csrf(client: TestClient) -> str:
    response = client.get("/")
    marker = 'name="csrf_token" value="'
    return response.text.split(marker, 1)[1].split('"', 1)[0]


def archive_videos(store: LocalStore, count: int) -> list[str]:
    records: list[dict[str, object]] = []
    video_ids: list[str] = []
    for index in range(count):
        video_id = str(7_700_000_000_000_000_000 + index)
        video_ids.append(video_id)
        records.append(
            {
                "video_id": video_id,
                "title": f"分页视频 {index:03d}",
                "video_url": f"https://www.douyin.com/video/{video_id}",
                "manifest_path": f"works/videos/douyin/{video_id}/manifest.json",
                "first_seen_at": "2026-09-07T10:00:00Z",
                "last_seen_at": (
                    f"2026-09-07T{10 + index // 60:02d}:{index % 60:02d}:00Z"
                ),
            }
        )
    store.upsert_videos(records)
    return video_ids


def rendered_video_ids(html: str) -> list[str]:
    return re.findall(r'name="video_id" value="(\d+)"', html)


def test_local_home_and_manual_authorization_job(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "本地归档" in response.text
        assert "brand/openbase-mark-reversed.svg" in response.text
        favicon_response = client.get("/favicon.ico")
        assert favicon_response.status_code == 200
        assert favicon_response.headers["content-type"] == "image/x-icon"
        assert favicon_response.content.startswith(b"\x00\x00\x01\x00")
        token = csrf(client)
        response = client.post(
            "/actions/authorize", data={"csrf_token": token}, follow_redirects=False
        )
        assert response.status_code == 303
    assert runner.calls == [("authorize", {})]


def test_local_home_paginates_video_archive_with_selectable_page_size(
    tmp_path: Path,
) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    video_ids = archive_videos(store, 105)
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        first = client.get("/")
        second = client.get("/?page=2&page_size=50")
        last = client.get("/?page=99&page_size=50")
        hundred = client.get("/?page=2&page_size=100")
        invalid = client.get("/?page=invalid&page_size=25")

    assert first.status_code == 200
    assert rendered_video_ids(first.text) == list(reversed(video_ids[75:]))
    assert "105 个视频" in first.text
    assert '<option value="30" selected>30</option>' in first.text
    assert 'aria-label="选择本页全部视频"' in first.text
    assert 'aria-label="视频档案分页"' in first.text
    assert '/?page_size=30&amp;page=2#video-archive' in first.text

    assert rendered_video_ids(second.text) == list(reversed(video_ids[5:55]))
    assert '<option value="50" selected>50</option>' in second.text
    assert '<span class="page-number is-current" aria-current="page">2</span>' in second.text

    assert rendered_video_ids(last.text) == list(reversed(video_ids[:5]))
    assert '<span class="page-number is-current" aria-current="page">3</span>' in last.text
    assert 'class="page-button is-disabled" aria-disabled="true"><span>下一页' in last.text

    assert rendered_video_ids(hundred.text) == list(reversed(video_ids[:5]))
    assert '<option value="100" selected>100</option>' in hundred.text
    assert rendered_video_ids(invalid.text) == list(reversed(video_ids[75:]))


def test_local_home_exposes_scoped_clear_dialog(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert client.app.version == __version__
    assert f'<span class="brand-version">v{__version__}</span>' in response.text
    assert ">清空数据</span>" in response.text
    assert 'action="/actions/clear-data"' in response.text
    assert 'name="scope" value="exports" checked' in response.text
    assert 'name="scope" value="comments"' in response.text
    assert 'name="scope" value="all"' in response.text
    assert "保留登录授权" in response.text


def test_clear_data_route_requires_confirmation_and_clears_selected_scope(
    tmp_path: Path,
) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    store.set_meta("last_export", {"directory": "exports/run-1"})
    export_file = local_settings.exports_dir / "run-1" / "table.xlsx"
    export_file.parent.mkdir(parents=True)
    export_file.write_bytes(b"export")
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        token = csrf(client)
        rejected = client.post(
            "/actions/clear-data",
            data={"csrf_token": token, "scope": "exports"},
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        assert export_file.is_file()
        assert "请先确认清空操作无法撤销" in client.get("/").text

        accepted = client.post(
            "/actions/clear-data",
            data={
                "csrf_token": csrf(client),
                "scope": "exports",
                "confirm_clear": "yes",
            },
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        home = client.get("/")

    assert not export_file.exists()
    assert local_settings.exports_dir.is_dir()
    assert store.get_meta("last_export", {}) == {}
    assert "已清空平台导出的原始数据" in home.text


def test_clear_data_route_requires_csrf(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    export_file = local_settings.exports_dir / "run-1" / "table.xlsx"
    export_file.parent.mkdir(parents=True)
    export_file.write_bytes(b"export")
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/clear-data",
            data={"scope": "exports", "confirm_clear": "yes"},
        )

    assert response.status_code == 403
    assert export_file.is_file()


def test_clear_data_route_warns_when_physical_deletion_is_pending(
    tmp_path: Path, monkeypatch
) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    monkeypatch.setattr(
        local_app_module,
        "clear_local_data",
        lambda *_args: ClearDataResult(
            scope="exports",
            files_deleted=1,
            directories_deleted=1,
            pending_directories=1,
        ),
    )

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/clear-data",
            data={
                "csrf_token": csrf(client),
                "scope": "exports",
                "confirm_clear": "yes",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        home = client.get("/")

    assert "暂存目录因文件占用未能删除" in home.text


def test_clear_data_route_rejects_active_jobs(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    export_file = local_settings.exports_dir / "run-1" / "table.xlsx"
    export_file.parent.mkdir(parents=True)
    export_file.write_bytes(b"export")
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        store.create_job("authorize")
        response = client.post(
            "/actions/clear-data",
            data={
                "csrf_token": csrf(client),
                "scope": "exports",
                "confirm_clear": "yes",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        home = client.get("/")

    assert export_file.is_file()
    assert "当前仍有任务等待或运行" in home.text


def test_export_requires_authorization(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/export",
            data={"csrf_token": csrf(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert runner.calls == []


def test_complete_export_requires_a_profile_sync(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    store.set_meta("creator_identity", {"handle": "creator-handle"})
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/export",
            data={"csrf_token": csrf(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert runner.calls == []


def test_complete_export_rejects_an_incomplete_profile_archive(
    tmp_path: Path,
) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    store.set_meta("creator_identity", {"handle": "creator-handle"})
    store.set_meta(
        "last_video_sync",
        {"finished_at": "2026-09-07T12:00:00Z", "complete": False},
    )
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/export",
            data={"csrf_token": csrf(client)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert runner.calls == []


def test_sync_route_always_targets_the_signed_in_profile(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    store.set_meta("creator_identity", {"handle": "creator-handle"})
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/actions/sync-videos",
            data={
                "csrf_token": csrf(client),
                "profile_url": "https://www.douyin.com/user/someone-else",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert runner.calls == [("sync_videos", {})]


def test_desktop_token_guards_ui_and_identifies_health_instance(tmp_path: Path) -> None:
    local_settings = settings(
        tmp_path, token="desktop-secret", instance_nonce="instance-123"
    )
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/favicon.ico").status_code == 200
        response = client.get(
            "/health", headers={"X-CSI-Desktop-Token": "desktop-secret"}
        )
        assert response.status_code == 200
        assert response.json()["instance_nonce"] == "instance-123"
        assert response.json()["version"] == __version__
        assert client.get("/").status_code == 200


def test_desktop_token_replaces_a_stale_cookie_after_restart(tmp_path: Path) -> None:
    local_settings = settings(tmp_path, token="new-desktop-secret")
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        client.cookies.set(
            "csi_desktop",
            "old-desktop-secret",
            domain="testserver.local",
            path="/",
        )
        response = client.get(
            "/", headers={"X-CSI-Desktop-Token": "new-desktop-secret"}
        )
        assert response.status_code == 200
        assert client.cookies.get(
            "csi_desktop", domain="testserver.local", path="/"
        ) == "new-desktop-secret"
        assert client.get("/static/local.css").status_code == 200


def test_shutdown_requires_desktop_token_and_invokes_callback(tmp_path: Path) -> None:
    stopped: list[bool] = []
    local_settings = settings(tmp_path, token="desktop-secret")
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(
            local_settings,
            store=store,
            runner=runner,
            shutdown_callback=lambda: stopped.append(True),
        )
    ) as client:
        assert client.post("/api/shutdown").status_code == 401
        response = client.post(
            "/api/shutdown",
            headers={"X-CSI-Desktop-Token": "desktop-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "stopping",
            "force_required": False,
        }
    assert stopped == [True]


def test_shutdown_is_not_exposed_in_source_mode(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        assert client.post("/api/shutdown").status_code == 404


def test_shutdown_waits_for_desktop_to_force_cleanup_of_active_browser_job(
    tmp_path: Path, monkeypatch
) -> None:
    stopped: list[bool] = []

    class ActiveRunner:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.close_calls = 0

        def start(self) -> None:
            return None

        def close(self) -> bool:
            self.close_calls += 1
            return False

    import admin_app.local_jobs as jobs_module

    monkeypatch.setattr(jobs_module, "LocalJobRunner", ActiveRunner)
    local_settings = settings(
        tmp_path, token="desktop-secret", instance_nonce="instance-123"
    )
    with TestClient(
        create_local_app(
            local_settings,
            shutdown_callback=lambda: stopped.append(True),
        )
    ) as client:
        response = client.post(
            "/api/shutdown",
            headers={"X-CSI-Desktop-Token": "desktop-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "status": "stopping",
            "force_required": True,
        }
        assert stopped == []


def test_comment_route_only_creates_explicit_immediate_job(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    store.upsert_videos(
        [
            {
                "video_id": "7680023068660346011",
                "title": "视频",
                "video_url": "https://www.douyin.com/video/7680023068660346011",
                "manifest_path": "works/videos/douyin/7680023068660346011/manifest.json",
                "first_seen_at": "2026-09-07T00:00:00Z",
                "last_seen_at": "2026-09-07T00:00:00Z",
                "visible_comment_count": 12_876,
            }
        ]
    )
    runner = FakeRunner(store)
    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "12,876" in home.text
        assert "尚未导出评论" in home.text
        assert ">导出评论</span>" in home.text
        assert (
            'formaction="/videos/7680023068660346011/comments"' in home.text
        )
        response = client.post(
            "/videos/7680023068660346011/comments",
            data={"csrf_token": csrf(client), "page": "2", "page_size": "50"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/?page=2&page_size=50#video-archive"
        )
    assert runner.calls == [
        ("comments", {"video_id": "7680023068660346011"})
    ]


def test_comment_batch_returns_to_the_current_video_page(tmp_path: Path) -> None:
    local_settings = settings(tmp_path)
    store = LocalStore(local_settings.database_path)
    video_ids = archive_videos(store, 2)
    runner = FakeRunner(store)

    with TestClient(
        create_local_app(local_settings, store=store, runner=runner)
    ) as client:
        response = client.post(
            "/comments/batch",
            data={
                "csrf_token": csrf(client),
                "video_id": video_ids,
                "page": "3",
                "page_size": "100",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/?page=3&page_size=100#video-archive"
    )
    assert runner.calls == [
        ("comments", {"video_id": video_id}) for video_id in video_ids
    ]
