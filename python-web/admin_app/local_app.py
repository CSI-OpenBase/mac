"""FastAPI UI for the focused, file-first CSI OpenBase workflow."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from . import __version__
from .local_cleanup import (
    CLEAR_DATA_LABELS,
    clear_local_data,
    recover_local_cleanup,
)
from .local_config import (
    COMMENT_EXPORT_DIRECTORY_KEY,
    LOCAL_PREFERENCES_META_KEY,
    LocalSettings,
    load_local_settings,
    prepare_comment_export_directory,
)
from .local_lock import WorkspaceLease
from .local_store import (
    GROUP_ID_RE,
    ActiveCommentJobError,
    ActiveLocalJobsError,
    LocalStore,
)
from .security import add_flash, csrf_token, pop_flashes, validate_csrf
from .viewmodels import pagination


APP_DIR = Path(__file__).resolve().parent
VIDEO_PAGE_SIZES = (30, 50, 100)
DEFAULT_VIDEO_PAGE_SIZE = VIDEO_PAGE_SIZES[0]
MAX_VIDEO_PAGE = 1_000_000


def _video_page(value: Any) -> int:
    try:
        parsed = int(str(value or "1"))
    except (TypeError, ValueError):
        return 1
    return min(max(parsed, 1), MAX_VIDEO_PAGE)


def _video_page_size(value: Any) -> int:
    try:
        parsed = int(str(value or DEFAULT_VIDEO_PAGE_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_PAGE_SIZE
    return parsed if parsed in VIDEO_PAGE_SIZES else DEFAULT_VIDEO_PAGE_SIZE


def _video_return_path(form: Any) -> str:
    page = _video_page(form.get("page"))
    page_size = _video_page_size(form.get("page_size"))
    query: dict[str, Any] = {"page": page, "page_size": page_size}
    group_id = _video_group_filter(form.get("group_id"))
    if group_id:
        query["group_id"] = group_id
    return f"/?{urlencode(query)}#video-archive"


def _video_group_filter(value: Any) -> str | None:
    group_id = str(value or "").strip()
    if group_id == "ungrouped" or GROUP_ID_RE.fullmatch(group_id):
        return group_id
    return None


class DesktopTokenMiddleware(BaseHTTPMiddleware):
    """Require the per-launch desktop secret when one was configured."""

    def __init__(self, app: Any, *, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if not self.token or request.url.path == "/favicon.ico":
            return await call_next(request)
        supplied = request.headers.get("X-CSI-Desktop-Token", "")
        cookie = request.cookies.get("csi_desktop", "")
        if not (
            (supplied and hmac.compare_digest(supplied, self.token))
            or (cookie and hmac.compare_digest(cookie, self.token))
        ):
            return HTMLResponse("Desktop session authentication failed", status_code=401)
        response = await call_next(request)
        if supplied and not hmac.compare_digest(cookie, self.token):
            response.set_cookie(
                "csi_desktop",
                self.token,
                httponly=True,
                samesite="strict",
                secure=False,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response


def _redirect(path: str = "/") -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def create_local_app(
    settings: LocalSettings | None = None,
    *,
    store: LocalStore | None = None,
    runner: Any | None = None,
    shutdown_callback: Any | None = None,
) -> FastAPI:
    settings = settings or load_local_settings()
    store = store or LocalStore(settings.database_path)
    owns_runner = runner is None
    if runner is None:
        from .local_jobs import LocalJobRunner

        runner = LocalJobRunner(store, settings)
    workspace_lease = WorkspaceLease(settings.data_home / ".openbase.instance.lock")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        release_workspace_lease = True
        workspace_lease.acquire()
        try:
            store.interrupt_active_jobs()
            recover_local_cleanup(settings, store)
            if hasattr(runner, "start"):
                runner.start()
            try:
                yield
            finally:
                if owns_runner and hasattr(runner, "close"):
                    stopped = runner.close()
                    if stopped is False:
                        release_workspace_lease = False
        finally:
            if release_workspace_lease:
                workspace_lease.release()

    app = FastAPI(
        title="CSI OpenBase",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.local_settings = settings
    app.state.local_runner = runner
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        DesktopTokenMiddleware, token=settings.desktop_token
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="strict",
        https_only=False,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

    def page_context(request: Request) -> dict[str, Any]:
        jobs = store.list_jobs(limit=30)
        requested_page = _video_page(request.query_params.get("page"))
        requested_page_size = _video_page_size(
            request.query_params.get("page_size")
        )
        requested_group_id = _video_group_filter(
            request.query_params.get("group_id")
        )
        groups = store.list_groups()
        known_groups = {str(group["group_id"]): group for group in groups}
        if requested_group_id not in {None, "ungrouped"} and requested_group_id not in known_groups:
            requested_group_id = None
        video_page = store.list_video_page(
            page=requested_page,
            page_size=requested_page_size,
            group_id=requested_group_id,
        )
        account = store.get_meta("creator_identity", {})
        last_discovery = store.get_meta("last_video_sync", {})
        return {
            "request": request,
            "app_version": __version__,
            "csrf_token": csrf_token(request),
            "flashes": pop_flashes(request),
            "account": account,
            "authorized": bool(account and account.get("handle")),
            "jobs": jobs,
            "videos": video_page["items"],
            "video_total": video_page["total"],
            "video_page_size": video_page["page_size"],
            "groups": groups,
            "manual_groups": [group for group in groups if group["source"] == "manual"],
            "selected_group_id": requested_group_id,
            "selected_group": known_groups.get(str(requested_group_id or "")),
            "video_pagination": pagination(
                page=video_page["page"],
                total_pages=video_page["pages"],
                total_items=video_page["total"],
                path="/",
                query={
                    "page_size": video_page["page_size"],
                    **(
                        {"group_id": requested_group_id}
                        if requested_group_id
                        else {}
                    ),
                },
                fragment="video-archive",
            ),
            "active_jobs": store.active_job_count(),
            "data_home": str(settings.data_home),
            "last_export": store.get_meta("last_export", {}),
            "last_discovery": last_discovery,
            "video_synced": last_discovery.get("complete") is True,
        }

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "mode": "local-archive",
                "active_jobs": store.active_job_count(),
                "instance_nonce": settings.instance_nonce,
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "local_home.html", page_context(request))

    @app.get("/settings", response_class=HTMLResponse)
    def local_settings_page(request: Request) -> HTMLResponse:
        preferences = store.get_meta(LOCAL_PREFERENCES_META_KEY, {})
        jobs = store.list_jobs(limit=1)
        comment_export_directory = (
            preferences.get(COMMENT_EXPORT_DIRECTORY_KEY, "")
            if isinstance(preferences, dict)
            else ""
        )
        account = store.get_meta("creator_identity", {})
        return templates.TemplateResponse(
            request,
            "local_settings.html",
            {
                "request": request,
                "app_version": __version__,
                "csrf_token": csrf_token(request),
                "flashes": pop_flashes(request),
                "account": account,
                "authorized": bool(account and account.get("handle")),
                "active_jobs": store.active_job_count(),
                "latest_job_id": jobs[0]["id"] if jobs else "",
                "comment_export_directory": str(comment_export_directory or ""),
                "default_comment_directory": str(
                    settings.works_dir / "videos" / "douyin" / "<视频ID>" / "comments"
                ),
            },
        )

    @app.post("/settings/comments")
    async def save_comment_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        if store.active_job_count():
            add_flash(request, "任务运行时不可修改设置", "warning")
            return _redirect("/settings")
        try:
            selected = prepare_comment_export_directory(
                str(form.get("comment_export_directory") or ""), settings
            )
        except (OSError, ValueError) as exc:
            add_flash(request, str(exc), "error")
            return _redirect("/settings")

        preferences = store.get_meta(LOCAL_PREFERENCES_META_KEY, {})
        if not isinstance(preferences, dict):
            preferences = {}
        preferences[COMMENT_EXPORT_DIRECTORY_KEY] = (
            str(selected) if selected is not None else ""
        )
        store.set_meta(LOCAL_PREFERENCES_META_KEY, preferences)
        message = (
            f"评论将额外导出到 {selected}"
            if selected is not None
            else "已恢复默认，仅保存在工作目录的视频档案中"
        )
        add_flash(request, message, "success")
        return _redirect("/settings")

    def submit(
        request: Request,
        kind: str,
        *,
        redirect_path: str = "/",
        **payload: Any,
    ) -> RedirectResponse:
        try:
            job = runner.submit(kind, **payload)
        except ActiveCommentJobError as exc:
            add_flash(request, str(exc), "warning")
        except (KeyError, ValueError, RuntimeError) as exc:
            add_flash(request, str(exc), "error")
        else:
            add_flash(request, f"任务 #{job['id']} 已开始", "success")
        return _redirect(redirect_path)

    @app.post("/actions/authorize")
    async def authorize(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        return submit(request, "authorize")

    @app.post("/actions/export")
    async def export_creator_data(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        if not store.get_meta("creator_identity", {}):
            add_flash(request, "请先完成创作者账号授权", "error")
            return _redirect()
        last_video_sync = store.get_meta("last_video_sync", {})
        if not last_video_sync:
            add_flash(request, "请先同步个人主页，再导出全部表格", "error")
            return _redirect()
        if last_video_sync.get("complete") is not True:
            add_flash(request, "个人主页档案不完整，请重新同步后再导出", "error")
            return _redirect()
        return submit(request, "export")

    @app.post("/actions/sync-videos")
    async def sync_videos(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        if not store.get_meta("creator_identity", {}):
            add_flash(request, "请先完成创作者账号授权", "error")
            return _redirect()
        return submit(request, "sync_videos")

    @app.post("/actions/clear-data")
    async def clear_data(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        scope = str(form.get("scope") or "").strip()
        if scope not in CLEAR_DATA_LABELS:
            add_flash(request, "请选择有效的数据清理范围", "error")
            return _redirect()
        if form.get("confirm_clear") != "yes":
            add_flash(request, "请先确认清空操作无法撤销", "warning")
            return _redirect()
        try:
            result = clear_local_data(settings, store, scope)
        except ActiveLocalJobsError as exc:
            add_flash(request, str(exc), "warning")
        except (OSError, RuntimeError, ValueError) as exc:
            add_flash(request, f"清空失败：{exc}", "error")
        else:
            if result.pending_directories:
                add_flash(
                    request,
                    (
                        f"已从当前归档清空{CLEAR_DATA_LABELS[scope]}；"
                        f"仍有 {result.pending_directories} 个暂存目录因文件占用"
                        "未能删除，请关闭占用程序后再次清空"
                    ),
                    "warning",
                )
            else:
                add_flash(
                    request,
                    (
                        f"已清空{CLEAR_DATA_LABELS[scope]}："
                        f"删除 {result.files_deleted} 个文件、"
                        f"{result.directories_deleted} 个目录"
                    ),
                    "success",
                )
        return _redirect()

    @app.post("/groups")
    async def create_group(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        try:
            group = store.create_group(str(form.get("name") or ""))
        except ValueError as exc:
            add_flash(request, str(exc), "error")
            return _redirect("/#video-archive")
        add_flash(request, f"已创建分组“{group['name']}”", "success")
        return _redirect(
            f"/?{urlencode({'group_id': group['group_id']})}#video-archive"
        )

    @app.post("/groups/{group_id}/rename")
    async def rename_group(request: Request, group_id: str) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        try:
            group = store.rename_group(group_id, str(form.get("name") or ""))
        except ValueError as exc:
            add_flash(request, str(exc), "error")
        else:
            add_flash(request, f"分组已重命名为“{group['name']}”", "success")
        return _redirect(_video_return_path(form))

    @app.post("/groups/{group_id}/delete")
    async def delete_group(request: Request, group_id: str) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        group = store.get_group(group_id)
        try:
            store.delete_group(group_id)
        except ValueError as exc:
            add_flash(request, str(exc), "error")
        else:
            name = str(group["name"]) if group else "该分组"
            add_flash(request, f"已删除分组“{name}”，作品档案仍然保留", "success")
        return _redirect("/#video-archive")

    async def change_group_videos(
        request: Request, *, add: bool
    ) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        raw_group_id = (
            form.get("target_group_id") if add else form.get("group_id")
        )
        group_id = str(raw_group_id or "").strip()
        video_ids = list(
            dict.fromkeys(str(value).strip() for value in form.getlist("video_id"))
        )
        try:
            if add:
                changed = store.add_videos_to_group(group_id, video_ids)
            else:
                changed = store.remove_videos_from_group(group_id, video_ids)
        except ValueError as exc:
            add_flash(request, str(exc), "error")
        else:
            action = "加入" if add else "移出"
            add_flash(request, f"已将 {changed} 个作品{action}分组", "success")
        return _redirect(_video_return_path(form))

    @app.post("/groups/videos/add")
    async def add_group_videos(request: Request) -> RedirectResponse:
        return await change_group_videos(request, add=True)

    @app.post("/groups/videos/remove")
    async def remove_group_videos(request: Request) -> RedirectResponse:
        return await change_group_videos(request, add=False)

    @app.post("/videos/{video_id}/comments")
    async def export_comments(request: Request, video_id: str) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        return submit(
            request,
            "comments",
            redirect_path=_video_return_path(form),
            video_id=video_id,
        )

    @app.post("/videos/{video_id}/comment-count")
    async def refresh_comment_count(
        request: Request, video_id: str
    ) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        return submit(
            request,
            "comment_count",
            redirect_path=_video_return_path(form),
            video_id=video_id,
        )

    @app.post("/comments/batch")
    async def export_comment_batch(request: Request) -> RedirectResponse:
        form = await request.form()
        validate_csrf(request, form)
        video_ids = list(dict.fromkeys(str(value).strip() for value in form.getlist("video_id")))
        video_ids = [value for value in video_ids if value]
        if not video_ids:
            add_flash(request, "请选择至少一个视频", "error")
            return _redirect(_video_return_path(form))
        accepted = 0
        rejected = 0
        for video_id in video_ids:
            try:
                runner.submit("comments", video_id=video_id)
                accepted += 1
            except (ActiveCommentJobError, KeyError, ValueError, RuntimeError):
                rejected += 1
        level = "warning" if rejected else "success"
        add_flash(request, f"已启动 {accepted} 个评论导出任务，跳过 {rejected} 个", level)
        return _redirect(_video_return_path(form))

    @app.get("/api/state")
    def state() -> JSONResponse:
        jobs = store.list_jobs(limit=30)
        return JSONResponse(
            {
                "active_jobs": store.active_job_count(),
                "latest_job_id": jobs[0]["id"] if jobs else None,
                "jobs": jobs,
            }
        )

    @app.post("/api/shutdown")
    def shutdown() -> JSONResponse:
        if not settings.desktop_token:
            raise HTTPException(status_code=404)
        if shutdown_callback is None:
            raise HTTPException(status_code=503)
        force_required = False
        if owns_runner and hasattr(runner, "close"):
            force_required = runner.close() is False
        if not force_required:
            shutdown_callback()
        return JSONResponse(
            {"status": "stopping", "force_required": force_required}
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(
            APP_DIR / "static" / "brand" / "favicon.ico",
            media_type="image/x-icon",
        )

    return app
