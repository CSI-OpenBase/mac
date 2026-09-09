"""Small security helpers for localhost forms."""

from __future__ import annotations

import hmac
import secrets
from typing import Any, Mapping

from fastapi import HTTPException, Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


CSRF_SESSION_KEY = "csrf_token"
FLASH_SESSION_KEY = "flash_messages"


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Stop oversized upload bodies while the multipart parser is still reading."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        paths: tuple[str, ...],
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in self.paths
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", status_code=400)(
                    scope, receive, send
                )
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("Upload exceeds the configured size limit", status_code=413)(
            scope, receive, send
        )


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf(request: Request, form: Mapping[str, Any]) -> None:
    expected = request.session.get(CSRF_SESSION_KEY, "")
    supplied = request.headers.get("X-CSRF-Token") or str(form.get("csrf_token", ""))
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


def add_flash(request: Request, message: str, level: str = "success") -> None:
    messages = list(request.session.get(FLASH_SESSION_KEY, []))
    messages.append({"message": message, "kind": level})
    request.session[FLASH_SESSION_KEY] = messages[-5:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages = list(request.session.get(FLASH_SESSION_KEY, []))
    request.session.pop(FLASH_SESSION_KEY, None)
    return messages
