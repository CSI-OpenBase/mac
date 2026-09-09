from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from admin_app.config import Settings
from admin_app import main as main_module
from admin_app.viewmodels import (
    datetime_display,
    datetime_iso,
    highlight_text,
    pagination,
    parse_local_datetime,
)


def test_pagination_clamps_page_and_preserves_nonempty_filters() -> None:
    pager = pagination(
        page=50,
        total_pages=8,
        total_items=153,
        path="/comments",
        query={
            "q": "双离合 & 保养",
            "status": "",
            "tag": ["维修", "反馈"],
            "page": 99,
        },
    )

    assert pager["page"] == 8
    assert pager["total_pages"] == 8
    assert pager["total_items"] == 153
    assert pager["next_url"] == ""
    assert any(item.get("ellipsis") for item in pager["items"])

    previous = urlsplit(pager["prev_url"])
    assert previous.path == "/comments"
    assert parse_qs(previous.query) == {
        "q": ["双离合 & 保养"],
        "tag": ["维修", "反馈"],
        "page": ["7"],
    }


def test_datetime_helpers_use_shanghai_for_display_and_utc_for_storage() -> None:
    utc_value = datetime(2026, 9, 6, 3, 20, tzinfo=timezone.utc)

    assert datetime_display(utc_value) == "2026-09-06 11:20"
    assert datetime_iso(utc_value) == "2026-09-06T11:20:00+08:00"
    assert datetime_display(datetime(2026, 9, 6, 3, 20)) == "2026-09-06 11:20"
    assert parse_local_datetime("2026-09-06T11:20") == datetime(2026, 9, 6, 3, 20)
    assert parse_local_datetime("") is None


def test_highlight_text_escapes_content_and_treats_query_literally() -> None:
    highlighted = highlight_text(
        '<img src=x onerror="alert(1)"> C++ 与 c++',
        "c++",
    )

    assert highlighted is not None
    rendered = str(highlighted)
    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert rendered.count("<mark>") == 2
    assert "<mark>C++</mark>" in rendered
    assert "<mark>c++</mark>" in rendered
    assert highlight_text("普通评论", "不存在") is None


class FakeRepository:
    def __init__(self) -> None:
        self.video_queries: list[dict[str, object]] = []
        self.created_jobs: list[dict[str, object]] = []

    def list_jobs(
        self, *, status: str | None = None, page: int = 1, page_size: int = 25
    ) -> dict[str, object]:
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 1}

    def list_import_runs(
        self, *, page: int = 1, page_size: int = 25
    ) -> dict[str, object]:
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "pages": 1,
        }

    def import_stats(self) -> dict[str, object]:
        return {
            "comment_count": 0,
            "snapshot_count": 0,
            "snapshot_growth": 0,
            "import_count": 0,
            "success_count": 0,
            "last_import_at": None,
            "last_source_path": "",
        }

    def list_videos(
        self,
        *,
        query: str | None = None,
        status: str | None = None,
        collection_id: str | None = None,
        sort: str = "priority",
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, object]:
        self.video_queries.append(
            {
                "query": query,
                "status": status,
                "collection_id": collection_id,
                "sort": sort,
                "page": page,
                "page_size": page_size,
            }
        )
        return {
            "items": [
                {
                    "video_id": "video-001",
                    "title": "离合器保养 <script>alert(1)</script>",
                    "collection_id": "collection-1",
                    "collection_name": "双离合知识库",
                    "episode": 1,
                    "status": "pending",
                    "stored_record_count": 12,
                    "visible_comment_count": 12,
                    "last_collected_at": datetime(2026, 9, 6, 3, 20),
                }
            ],
            "total": 1,
            "page": page,
            "page_size": page_size,
            "pages": 1,
        }

    def dashboard_stats(self) -> dict[str, object]:
        return {
            "target_video_count": 1,
            "complete_video_count": 0,
            "pending_video_count": 1,
            "partial_video_count": 0,
            "blocked_video_count": 0,
            "running_job_count": 0,
            "queued_job_count": 0,
        }

    def list_collections(self) -> list[dict[str, object]]:
        return [{"collection_id": "collection-1", "name": "双离合知识库"}]

    def create_job(self, job_type: str, **values) -> dict[str, object]:
        job = {"id": 99, "job_type": job_type, **values}
        self.created_jobs.append(job)
        return job


def test_videos_page_uses_repository_filters_and_csrf_rejects_missing_token(
    tmp_path, monkeypatch
) -> None:
    repository = FakeRepository()
    monkeypatch.setattr(main_module, "Repository", lambda _engine: repository)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = Settings(
        repository_root=tmp_path,
        db_name="test_douyin",
        db_password="unused",
        session_secret="test-session-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    app = main_module.create_app(
        settings,
        engine=engine,
        migrate=False,
        start_worker=False,
    )
    assert app.title == "CSI OpenBase"

    with TestClient(app) as client:
        analysis_response = client.get("/analysis")
        response = client.get(
            "/videos",
            params={"q": "离合器", "status": "pending", "collection_id": "collection-1"},
        )

        assert analysis_response.status_code == 200
        assert "CSI OpenBase" in analysis_response.text
        assert "前往 CSI 深度分析" in analysis_response.text
        assert "当前缺少：账号画像、作品表现、受众画像、评论语料。" in analysis_response.text
        assert re.search(r"20\d\d-\d\d-\d\d \d\d:\d\d", analysis_response.text)
        assert response.status_code == 200
        assert "视频采集" in response.text
        assert "离合器保养" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert repository.video_queries[-1] == {
            "query": "离合器",
            "status": "pending",
            "collection_id": "collection-1",
            "sort": "priority",
            "page": 1,
            "page_size": 25,
        }

        rejected = client.post("/analysis/run", data={})

        token_match = re.search(
            r'<meta name="csrf-token" content="([^"]+)">', response.text
        )
        assert token_match is not None
        accepted = client.post(
            "/analysis/run",
            data={"csrf_token": token_match.group(1)},
            follow_redirects=False,
        )

    engine.dispose()
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "CSRF validation failed"}
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/jobs"
    assert repository.created_jobs == [{"id": 99, "job_type": "analyze"}]


def test_external_worker_startup_still_recovers_archive_transaction(
    tmp_path, monkeypatch
) -> None:
    recovered: list[object] = []

    class BusyWorkerLock:
        def __init__(self, _path) -> None:
            pass

        def __enter__(self):
            raise main_module.WorkerAlreadyRunning("worker is already active")

    @contextmanager
    def tracking_archive_lock(path):
        recovered.append(path)
        yield

    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = Settings(
        repository_root=tmp_path,
        db_name="test_douyin",
        db_password="unused",
        session_secret="test-session-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    monkeypatch.setattr(main_module, "WorkerInstanceLock", BusyWorkerLock)
    monkeypatch.setattr(main_module, "archive_lock", tracking_archive_lock)
    app = main_module.create_app(settings, engine=engine, migrate=False)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["version"] == main_module.__version__
        assert app.version == main_module.__version__
        assert app.state.worker_mode == "external"

    assert recovered == [settings.comments_dir / ".archive.lock"]
    engine.dispose()


def test_target_upload_route_requires_confirmation_and_updates_workspace(
    tmp_path, monkeypatch
) -> None:
    repository = FakeRepository()
    monkeypatch.setattr(main_module, "Repository", lambda _engine: repository)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    settings = Settings(
        repository_root=tmp_path,
        workspace_slug="creator-one",
        workspace_dir=tmp_path / "creator-one",
        db_name="test_douyin",
        db_password="unused",
        session_secret="test-session-secret",
        browser_profile_dir=tmp_path / "browser-profile",
    )
    app = main_module.create_app(
        settings,
        engine=engine,
        migrate=False,
        start_worker=False,
    )
    video_id = "7654321098765432109"
    payload = json.dumps(
        {
            "videos": [
                {
                    "video_url": f"https://www.douyin.com/video/{video_id}",
                    "title": "新账号作品",
                }
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")

    with TestClient(app) as client:
        page = client.get("/imports")
        assert page.status_code == 200
        assert 'action="/imports/targets"' in page.text
        assert "下载模板" in page.text
        token_match = re.search(
            r'<meta name="csrf-token" content="([^"]+)">', page.text
        )
        assert token_match is not None
        token = token_match.group(1)
        rejected = client.post(
            "/imports/targets",
            data={"csrf_token": token},
            files={"file": ("targets.json", payload, "application/json")},
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        assert not settings.targets_path.exists()

        accepted = client.post(
            "/imports/targets",
            data={"csrf_token": token, "confirm_replace": "1"},
            files={"file": ("targets.json", payload, "application/json")},
            follow_redirects=False,
        )
        template = client.get("/imports/targets/template")

    engine.dispose()
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/imports"
    stored = json.loads(settings.targets_path.read_text(encoding="utf-8"))
    assert [item["video_id"] for item in stored["videos"]] == [video_id]
    assert template.status_code == 200
    assert "attachment" in template.headers["content-disposition"]
