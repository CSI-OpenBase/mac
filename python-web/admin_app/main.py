"""FastAPI application for one local CSI OpenBase creator workspace."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .archive_lock import archive_lock
from .account_analysis import build_account_analysis
from .account_data import AccountDataError
from .config import Settings, load_settings
from .database import (
    assert_existing_workspace_identity,
    bind_workspace_identity,
    initialize_database_engine,
    upgrade_database,
    verify_schema_contract,
)
from .jobs import (
    JobWorker,
    WorkerAlreadyRunning,
    WorkerInstanceLock,
    build_background_scheduler,
)
from .repository import Repository
from .security import (
    RequestBodyLimitMiddleware,
    add_flash,
    csrf_token,
    pop_flashes,
    validate_csrf,
)
from .services import (
    CommentDataError,
    TargetDataError,
    ingest_batch_bytes,
    ingest_account_snapshot_bytes,
    ingest_creator_export_bytes,
    ingest_target_manifest_bytes,
    sync_mysql_archive,
    update_collection_progress,
)
from .work_data import WorkDataError
from .viewmodels import (
    STATUS_LABELS,
    decorate_comment,
    decorate_import,
    decorate_job,
    decorate_video,
    datetime_display,
    datetime_iso,
    pagination,
    parse_local_datetime,
    video_title_map,
)


APP_DIR = Path(__file__).resolve().parent


def _redirect(path: str, *, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(path, status_code=status_code)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _action_response(request: Request, destination: str = "/jobs") -> Response:
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": destination})
    return _redirect(destination)


def _safe_page(value: int) -> int:
    return max(1, value)


def _common_context(
    request: Request, repository: Repository, settings: Settings
) -> dict[str, Any]:
    try:
        running = repository.list_jobs(status="running", page=1, page_size=1)["total"]
        connected = True
    except Exception:
        running, connected = 0, False
    return {
        "request": request,
        "app_version": __version__,
        "csrf_token": csrf_token(request),
        "flashes": pop_flashes(request),
        "nav_running_jobs": running,
        "db_connected": connected,
        "workspace": {
            "slug": settings.workspace_slug,
            "display_name": settings.workspace_name,
            "platform": settings.platform,
        },
    }


def _decorate_jobs(repository: Repository, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    videos = repository.list_videos(page=1, page_size=100)["items"]
    titles = video_title_map(videos)
    return [decorate_job(item, video_titles=titles) for item in items]


def _source_rows(settings: Settings, stats: Mapping[str, Any]) -> list[dict[str, Any]]:
    def jsonl_count(path: Path) -> int:
        if not path.exists():
            return 0
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                return sum(bool(line.strip()) for line in handle)
        except OSError:
            return 0

    definitions = [
        ("评论主档", settings.canonical_comments_path, "file-json", stats.get("comment_count", 0)),
        ("视频目标", settings.targets_path, "list-video", stats.get("target_video_count", 0)),
        ("采集进度", settings.progress_path, "scan-search", None),
        ("作品快照", settings.work_snapshots_path, "bar-chart-3", jsonl_count(settings.work_snapshots_path)),
        ("账号画像", settings.profile_snapshots_path, "user-round", jsonl_count(settings.profile_snapshots_path)),
        ("受众画像", settings.audience_snapshots_path, "users-round", jsonl_count(settings.audience_snapshots_path)),
    ]
    rows: list[dict[str, Any]] = []
    for name, path, icon, count in definitions:
        if count is None and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                count = len(payload.get("videos", {}))
            except (OSError, ValueError, TypeError):
                count = 0
        modified = (
            datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.now().astimezone().tzinfo)
            if path.exists()
            else None
        )
        try:
            display_path = path.relative_to(settings.data_dir).as_posix()
        except ValueError:
            display_path = path.name
        rows.append(
            {
                "name": name,
                "path": display_path,
                "icon": icon,
                "record_count": int(count or 0),
                "record_count_display": f"{int(count or 0):,} 条",
                "updated_at": datetime_iso(modified),
                "updated_at_display": datetime_display(modified),
                "status": "success" if path.exists() else "failed",
                "status_label": "正常" if path.exists() else "缺失",
            }
        )
    return rows


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    migrate: bool = True,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    owns_engine = engine is None
    if engine is None:
        engine = initialize_database_engine(settings, migrate=migrate)
    elif migrate:
        assert_existing_workspace_identity(engine, settings)
        upgrade_database(engine)
        verify_schema_contract(engine)
        bind_workspace_identity(engine, settings)
    repository = Repository(engine)
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        scheduler = None
        worker_lock = None
        try:
            if start_worker:
                lock_path = (
                    settings.browser_profile_dir.parent
                    / f"{settings.workspace_slug}.collection-worker.lock"
                )
                candidate_lock = WorkerInstanceLock(lock_path)
                try:
                    candidate_lock.__enter__()
                except WorkerAlreadyRunning:
                    application.state.worker_mode = "external"
                else:
                    worker_lock = candidate_lock
                with archive_lock(settings.comments_dir / ".archive.lock"):
                    pass
                if worker_lock is not None:
                    repository.requeue_stale_jobs(stale_after=None)
                    worker = JobWorker(repository, settings)
                    scheduler = build_background_scheduler(
                        worker, poll_seconds=settings.job_poll_seconds
                    )
                    scheduler.start()
                    application.state.worker_mode = "embedded"
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=True)
            if worker_lock is not None:
                worker_lock.__exit__(None, None, None)
            if owns_engine:
                engine.dispose()

    app = FastAPI(
        title="CSI OpenBase",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.repository = repository
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.max_upload_bytes + 256 * 1024,
        paths=(
            "/imports/upload",
            "/imports/targets",
            "/imports/works",
            "/imports/account",
        ),
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="strict",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def render(request: Request, template: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={**_common_context(request, repository, settings), **context},
        )

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return JSONResponse(
                {
                    "status": "ok",
                    "version": __version__,
                    "workspace": settings.workspace_slug,
                    "database": settings.db_name,
                }
            )
        except Exception:
            return JSONResponse(
                {
                    "status": "degraded",
                    "version": __version__,
                    "workspace": settings.workspace_slug,
                    "database": settings.db_name,
                },
                status_code=503,
            )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        raw = repository.dashboard_stats()
        account = build_account_analysis(settings)
        pending = repository.list_videos(status="pending", page=1, page_size=6)
        jobs_page = repository.list_jobs(page=1, page_size=5)
        imports_page = repository.list_import_runs(page=1, page_size=5)
        collections = repository.list_collections()
        jobs = _decorate_jobs(repository, jobs_page["items"])
        imports = [decorate_import(item) for item in imports_page["items"]]
        stats = {
            "coverage_percent": round(float(raw["coverage_ratio"]) * 100, 1),
            "completed_videos": raw["complete_video_count"],
            "total_videos": raw["target_video_count"],
            "pending_videos": raw["pending_video_count"],
            "partial_videos": raw["partial_video_count"],
            "blocked_videos": raw["blocked_video_count"],
            "comment_count": raw["comment_count"],
            "root_count": raw["root_comment_count"],
            "reply_count": raw["reply_comment_count"],
            "creator_count": raw["creator_comment_count"],
            "viewer_count": raw["viewer_comment_count"],
            "snapshot_count": raw["snapshot_count"],
            "running_jobs": raw["running_job_count"],
            "last_import_display": datetime_display(raw.get("last_import_at")),
            "work_count": account["works"]["analyzed_work_count"],
            "total_views": account["works"]["total_views"],
            "audience_dimensions": len(account["audience"]["dimensions"]),
        }
        scope_name = " · ".join(str(item["name"]) for item in collections)
        return render(
            request,
            "dashboard.html",
            stats=stats,
            scope_name=scope_name or settings.workspace_name,
            updated_at_display=datetime_display(raw.get("last_import_at")),
            next_videos=[decorate_video(item) for item in pending["items"]],
            jobs=jobs,
            recent_imports=imports,
            account_analysis=account,
        )

    @app.get("/analysis", response_class=HTMLResponse)
    def account_analysis(request: Request) -> HTMLResponse:
        analysis = build_account_analysis(settings)
        generated_at = datetime.fromisoformat(
            str(analysis["generated_at"]).replace("Z", "+00:00")
        )
        analysis["generated_at_display"] = datetime_display(
            generated_at
        )
        return render(
            request,
            "analysis.html",
            analysis=analysis,
        )

    @app.get("/videos", response_class=HTMLResponse)
    def videos(
        request: Request,
        q: str = "",
        status: str = "",
        collection_id: str = "",
        sort: str = "priority",
        page: int = 1,
    ) -> HTMLResponse:
        page = _safe_page(page)
        if status not in {"", "pending", "partial", "complete", "blocked"}:
            status = ""
        if sort not in {"priority", "episode", "comments_desc", "updated_desc"}:
            sort = "priority"
        result = repository.list_videos(
            query=q or None,
            status=status or None,
            collection_id=collection_id or None,
            sort=sort,
            page=page,
            page_size=25,
        )
        raw_stats = repository.dashboard_stats()
        filters = {"q": q, "status": status, "collection_id": collection_id, "sort": sort}
        pager = pagination(
            page=result["page"], total_pages=result["pages"], total_items=result["total"],
            path="/videos", query=filters,
        )
        return render(
            request,
            "videos.html",
            videos=[decorate_video(item) for item in result["items"]],
            collections=repository.list_collections(),
            filters=filters,
            stats={
                "total": raw_stats["target_video_count"],
                "complete": raw_stats["complete_video_count"],
                "pending": raw_stats["pending_video_count"],
                "partial": raw_stats["partial_video_count"],
                "blocked": raw_stats["blocked_video_count"],
            },
            pagination=pager,
        )

    @app.get("/videos/{video_id}", response_class=HTMLResponse)
    def video_detail(request: Request, video_id: str) -> HTMLResponse:
        raw_video = repository.get_video(video_id)
        if raw_video is None:
            raise HTTPException(status_code=404, detail="video not found")
        video = decorate_video(raw_video)
        progress = list(raw_video.get("progress") or [])
        if progress:
            current = progress[0]
            for key in ("last_batch", "visible_comment_count", "stored_record_count", "notes"):
                video[key] = current.get(key)
            video["last_collected_at"] = datetime_iso(current.get("last_collected_at"))
            video["last_collected_display"] = datetime_display(current.get("last_collected_at"))
        comments_page = repository.search_comments(
            video_id=video_id, comment_type="root", sort="likes", page=1, page_size=10
        )
        raw_stats = raw_video["comment_stats"]
        history = []
        for item in raw_video.get("recent_jobs") or []:
            if item.get("job_type") != "collect_video":
                continue
            result = item.get("result") or {}
            capture = result.get("capture") or {} if isinstance(result, Mapping) else {}
            history.append(
                {
                    "status": item.get("status", "queued"),
                    "status_label": STATUS_LABELS.get(str(item.get("status")), str(item.get("status"))),
                    "collected_at": datetime_iso(item.get("finished_at") or item.get("created_at")),
                    "collected_at_display": datetime_display(item.get("finished_at") or item.get("created_at")),
                    "record_count": int(capture.get("captured_records") or 0),
                    "batch": capture.get("batch_name"),
                }
            )
        return render(
            request,
            "video_detail.html",
            video=video,
            stats={
                "comment_count": raw_stats["comment_count"],
                "root_count": raw_stats["root_comment_count"],
                "reply_count": raw_stats["reply_comment_count"],
                "creator_count": raw_stats["creator_comment_count"],
                "snapshot_count": raw_stats["snapshot_count"],
            },
            comments=[decorate_comment(item) for item in comments_page["items"]],
            collection_history=history,
        )

    @app.post("/videos/{video_id}/collect")
    async def collect_video(request: Request, video_id: str) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        if repository.get_video(video_id) is None:
            raise HTTPException(status_code=404, detail="video not found")
        job = repository.create_job("collect_video", video_id=video_id)
        add_flash(request, f"已创建采集任务 #{job['id']}")
        return _action_response(request)

    @app.post("/videos/{video_id}/status")
    async def save_video_status(request: Request, video_id: str) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        video = repository.get_video(video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        current = (video.get("progress") or [{}])[0]
        update_collection_progress(
            settings,
            video_id=video_id,
            status=str(form.get("status") or ""),
            visible_comment_count=int(current.get("visible_comment_count") or 0),
            notes=str(current.get("notes") or ""),
        )
        sync_mysql_archive(settings)
        add_flash(request, "采集状态已保存")
        return _redirect(f"/videos/{video_id}")

    @app.get("/comments", response_class=HTMLResponse)
    def comments(
        request: Request,
        q: str = "",
        video_id: str = "",
        collection_id: str = "",
        author_role: str = "",
        comment_type: str = "",
        sort: str = "collected_desc",
        page: int = 1,
    ) -> HTMLResponse:
        sort_map = {
            "collected_desc": "collected",
            "likes_desc": "likes",
            "replies_desc": "replies",
            "published_desc": "newest",
        }
        if sort not in sort_map:
            sort = "collected_desc"
        if author_role not in {"", "viewer", "creator"}:
            author_role = ""
        if comment_type not in {"", "root", "reply"}:
            comment_type = ""
        page = _safe_page(page)
        result = repository.search_comments(
            query=q or None,
            video_id=video_id or None,
            collection_id=collection_id or None,
            author_role=author_role or None,
            comment_type=comment_type or None,
            sort=sort_map[sort],
            page=page,
            page_size=40,
        )
        filters = {
            "q": q,
            "video_id": video_id,
            "collection_id": collection_id,
            "author_role": author_role,
            "comment_type": comment_type,
            "sort": sort,
        }
        pager = pagination(
            page=result["page"], total_pages=result["pages"], total_items=result["total"],
            path="/comments", query=filters,
        )
        items = [decorate_comment(item, query=q) for item in result["items"]]
        all_videos = [decorate_video(item) for item in repository.list_videos(page=1, page_size=100)["items"]]
        return render(
            request,
            "comments.html",
            comments=items,
            filters=filters,
            video_options=all_videos,
            collections=repository.list_collections(),
            stats={"total": result["total"], "last_collected_display": datetime_display(result.get("last_collected_at"))},
            pagination=pager,
        )

    @app.get("/comments/{comment_id}", response_class=HTMLResponse)
    def comment_thread(request: Request, comment_id: str) -> HTMLResponse:
        thread = repository.get_comment_thread(comment_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="comment not found")
        selected = repository.get_comment(comment_id) or {}
        root = decorate_comment({**thread["root"], "video_title": selected.get("video_title")})
        replies = []
        root_id = str(root["comment_id"])
        for item in thread["replies"]:
            decorated = decorate_comment(item)
            decorated["parent_is_root"] = str(item.get("parent_comment_id") or root_id) == root_id
            decorated["depth"] = 1 if decorated["parent_is_root"] else 2
            replies.append(decorated)
        snapshot_page = repository.list_comment_snapshots(root_id, page=1, page_size=50)
        snapshots = []
        raw_snapshots = snapshot_page["items"]
        for index, item in enumerate(raw_snapshots):
            row = dict(item)
            older = raw_snapshots[index + 1] if index + 1 < len(raw_snapshots) else None
            row["collected_at_display"] = datetime_display(row.get("collected_at"))
            row["collected_at"] = datetime_iso(row.get("collected_at"))
            row["like_delta"] = int(row.get("like_count") or 0) - int(older.get("like_count") or 0) if older else 0
            row["reply_delta"] = int(row.get("reply_count") or 0) - int(older.get("reply_count") or 0) if older else 0
            row["text_changed"] = bool(older and row.get("text") != older.get("text"))
            snapshots.append(row)
        return render(
            request,
            "comment_thread.html",
            root_comment=root,
            replies=replies,
            snapshots=snapshots,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, status: str = "", sort: str = "newest", page: int = 1) -> HTMLResponse:
        page = _safe_page(page)
        if status not in {"", "queued", "running", "succeeded", "failed", "blocked", "cancelled"}:
            status = ""
        if sort not in {"newest", "scheduled", "status"}:
            sort = "newest"
        result = repository.list_jobs(status=status or None, sort=sort, page=page, page_size=25)
        videos_page = repository.list_videos(page=1, page_size=100)
        videos = [decorate_video(item) for item in videos_page["items"]]
        filters = {"status": status, "sort": sort}
        pager = pagination(
            page=result["page"], total_pages=result["pages"], total_items=result["total"],
            path="/jobs", query=filters,
        )
        raw_stats = repository.dashboard_stats()
        return render(
            request,
            "jobs.html",
            jobs=[decorate_job(item, video_titles=video_title_map(videos)) for item in result["items"]],
            video_options=videos,
            default_scheduled_for=datetime.now().strftime("%Y-%m-%dT%H:%M"),
            filters=filters,
            stats={"running": raw_stats["running_job_count"], "queued": raw_stats["queued_job_count"]},
            pagination=pager,
            jobs_status=status,
            jobs_sort=sort,
            jobs_page=page,
            jobs_limit=25,
        )

    @app.post("/jobs")
    async def create_job(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        job_type = str(form.get("job_type") or "collect_video")
        if job_type not in {"collect_video", "sync_mysql", "analyze"}:
            raise HTTPException(status_code=400, detail="invalid job type")
        video_ids = [str(value).strip() for value in form.getlist("video_id") if str(value).strip()]
        if job_type == "collect_video" and not video_ids:
            add_flash(request, "请选择目标视频", "error")
            return _redirect("/jobs")
        try:
            scheduled_for = parse_local_datetime(str(form.get("scheduled_for") or ""))
        except ValueError:
            add_flash(request, "计划执行时间格式无效", "error")
            return _redirect("/jobs")
        created = []
        for video_id in video_ids or [None]:
            if video_id and repository.get_video(video_id) is None:
                raise HTTPException(status_code=404, detail=f"video {video_id} not found")
            created.append(repository.create_job(job_type, video_id=video_id, scheduled_for=scheduled_for))
        add_flash(request, f"已创建 {len(created)} 个任务")
        return _redirect("/jobs")

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        request: Request,
        job_id: int,
        status: str = "",
        sort: str = "newest",
        page: int = 1,
        limit: int = 25,
    ) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        cancelled = repository.cancel_job(job_id, message="由本地后台取消")
        if _is_htmx(request):
            if status not in {"", "queued", "running", "succeeded", "failed", "blocked", "cancelled"}:
                status = ""
            if sort not in {"newest", "scheduled", "status"}:
                sort = "newest"
            page = max(1, page)
            limit = min(max(1, limit), 100)
            result = repository.list_jobs(
                status=status or None, sort=sort, page=page, page_size=limit
            )
            return templates.TemplateResponse(
                request=request,
                name="partials/jobs_table.html",
                context={
                    "request": request,
                    "csrf_token": csrf_token(request),
                    "jobs": _decorate_jobs(repository, result["items"]),
                    "jobs_status": status,
                    "jobs_sort": sort,
                    "jobs_page": page,
                    "jobs_limit": limit,
                },
            )
        add_flash(request, "任务已取消" if cancelled else "该任务当前不能取消", "success" if cancelled else "warning")
        return _redirect("/jobs")

    @app.get("/partials/jobs", response_class=HTMLResponse)
    def jobs_partial(
        request: Request,
        status: str = "",
        sort: str = "newest",
        page: int = 1,
        limit: int = 25,
    ) -> HTMLResponse:
        limit = min(max(1, limit), 100)
        page = max(1, page)
        if status not in {"", "queued", "running", "succeeded", "failed", "blocked", "cancelled"}:
            status = ""
        if sort not in {"newest", "scheduled", "status"}:
            sort = "newest"
        result = repository.list_jobs(
            status=status or None, sort=sort, page=page, page_size=limit
        )
        return templates.TemplateResponse(
            request=request,
            name="partials/jobs_table.html",
            context={
                "request": request,
                "csrf_token": csrf_token(request),
                "jobs": _decorate_jobs(repository, result["items"]),
                "jobs_status": status,
                "jobs_sort": sort,
                "jobs_page": page,
                "jobs_limit": limit,
            },
        )

    @app.get("/imports", response_class=HTMLResponse)
    def imports(request: Request, page: int = 1) -> HTMLResponse:
        page = _safe_page(page)
        result = repository.list_import_runs(page=page, page_size=25)
        aggregate = repository.import_stats()
        raw_stats = repository.dashboard_stats()
        decorated = [decorate_import(item) for item in result["items"]]
        pager = pagination(
            page=result["page"], total_pages=result["pages"], total_items=result["total"],
            path="/imports", query={},
        )
        return render(
            request,
            "imports.html",
            imports=decorated,
            stats={
                "comment_count": aggregate["comment_count"],
                "snapshot_count": aggregate["snapshot_count"],
                "snapshot_growth": aggregate["snapshot_growth"],
                "import_count": aggregate["import_count"],
                "success_count": aggregate["success_count"],
                "last_import_display": datetime_display(aggregate.get("last_import_at")),
                "last_source_name": Path(str(aggregate.get("last_source_path") or "")).name or "暂无来源",
            },
            sources=_source_rows(settings, raw_stats),
            validation=None,
            pagination=pager,
        )

    @app.post("/imports/upload")
    async def upload_import(request: Request) -> Response:
        form = await request.form(max_part_size=settings.max_upload_bytes + 1)
        validate_csrf(request, form)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            add_flash(request, "请选择 JSONL 文件", "error")
            return _redirect("/imports")
        content = await upload.read(settings.max_upload_bytes + 1)
        try:
            result = ingest_batch_bytes(
                content, getattr(upload, "filename", None), settings
            )
        except (CommentDataError, UnicodeError, OSError) as exc:
            add_flash(request, f"导入失败：{exc}", "error")
            return _redirect("/imports")
        if form.get("sync_after_import"):
            try:
                sync_mysql_archive(settings)
            except Exception:
                add_flash(request, "批次已归档，但 MySQL 同步失败；请稍后重新同步", "warning")
                return _redirect("/imports")
        if form.get("analyze_after_import"):
            repository.create_job("analyze")
        add_flash(request, f"已归档批次 {result['batch_name']}，当前共 {result['stored_records']:,} 条评论")
        return _redirect("/imports")

    @app.get("/imports/targets/template")
    def target_import_template() -> JSONResponse:
        return JSONResponse(
            {
                "videos": [
                    {
                        "video_id": "填写8到32位作品ID",
                        "video_url": "https://www.douyin.com/video/填写同一作品ID",
                        "title": "作品标题",
                    }
                ]
            },
            headers={
                "Content-Disposition": 'attachment; filename="collection-targets.template.json"'
            },
        )

    @app.post("/imports/targets")
    async def upload_targets(request: Request) -> Response:
        form = await request.form(max_part_size=settings.max_upload_bytes + 1)
        validate_csrf(request, form)
        if form.get("confirm_replace") != "1":
            add_flash(request, "请确认替换当前视频目标清单", "error")
            return _redirect("/imports")
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            add_flash(request, "请选择 JSON 目标清单", "error")
            return _redirect("/imports")
        content = await upload.read(settings.max_upload_bytes + 1)
        try:
            result = ingest_target_manifest_bytes(
                content, getattr(upload, "filename", None), settings
            )
        except (TargetDataError, OSError) as exc:
            add_flash(request, f"视频目标导入失败：{exc}", "error")
            return _redirect("/imports")
        if form.get("sync_after_import"):
            try:
                sync_mysql_archive(settings)
            except Exception:
                add_flash(
                    request,
                    "视频目标已更新，但 MySQL 同步失败；请稍后重新同步",
                    "warning",
                )
                return _redirect("/imports")
        add_flash(
            request,
            f"已载入 {result['target_video_count']:,} 个视频目标，"
            f"保留 {result['preserved_progress_count']:,} 条采集进度",
        )
        return _redirect("/imports")

    @app.post("/imports/works")
    async def upload_works(request: Request) -> Response:
        form = await request.form(max_part_size=settings.max_upload_bytes + 1)
        validate_csrf(request, form)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            add_flash(request, "请选择 XLSX 或 CSV 文件", "error")
            return _redirect("/imports")
        content = await upload.read(settings.max_upload_bytes + 1)
        try:
            result = ingest_creator_export_bytes(
                content, getattr(upload, "filename", None), settings
            )
        except (WorkDataError, UnicodeError, OSError) as exc:
            add_flash(request, f"作品数据导入失败：{exc}", "error")
            return _redirect("/imports")
        if form.get("sync_after_import"):
            try:
                sync_mysql_archive(settings)
            except Exception:
                add_flash(
                    request,
                    "作品快照已归档，但 MySQL 同步失败；请稍后重新同步",
                    "warning",
                )
                return _redirect("/imports")
        if form.get("analyze_after_import"):
            repository.create_job("analyze")
        add_flash(
            request,
            f"已导入 {result['incoming_snapshots']:,} 条作品快照，"
            f"覆盖 {result['work_count']:,} 条作品",
        )
        return _redirect("/imports")

    @app.post("/imports/account")
    async def upload_account_snapshot(request: Request) -> Response:
        form = await request.form(max_part_size=settings.max_upload_bytes + 1)
        validate_csrf(request, form)
        kind = str(form.get("kind") or "")
        upload = form.get("file")
        if kind not in {"profile", "audience"}:
            add_flash(request, "请选择账号画像或受众画像", "error")
            return _redirect("/imports")
        if upload is None or not hasattr(upload, "read"):
            add_flash(request, "请选择 JSONL 文件", "error")
            return _redirect("/imports")
        content = await upload.read(settings.max_upload_bytes + 1)
        try:
            result = ingest_account_snapshot_bytes(
                content, kind=kind, settings=settings
            )
        except (AccountDataError, UnicodeError, OSError) as exc:
            add_flash(request, f"画像数据导入失败：{exc}", "error")
            return _redirect("/imports")
        if form.get("sync_after_import"):
            try:
                sync_mysql_archive(settings)
            except Exception:
                add_flash(
                    request,
                    "画像快照已归档，但 MySQL 同步失败；请稍后重新同步",
                    "warning",
                )
                return _redirect("/imports")
        if form.get("analyze_after_import"):
            repository.create_job("analyze")
        label = "账号画像" if kind == "profile" else "受众画像"
        add_flash(
            request,
            f"已导入 {result['incoming_snapshots']:,} 条{label}快照",
        )
        return _redirect("/imports")

    @app.post("/imports/sync")
    async def enqueue_sync(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        job = repository.create_job("sync_mysql")
        add_flash(request, f"已创建数据库同步任务 #{job['id']}")
        return _action_response(request)

    @app.post("/analysis/run")
    async def enqueue_analysis(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form)
        job = repository.create_job("analyze")
        add_flash(request, f"已创建账号分析任务 #{job['id']}")
        return _action_response(request)

    return app


app: FastAPI | None = None
