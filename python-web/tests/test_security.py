from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from admin_app.security import RequestBodyLimitMiddleware


UPLOAD_PATHS = (
    "/imports/upload",
    "/imports/targets",
    "/imports/works",
    "/imports/account",
)
MAX_BODY_BYTES = 8


def _upload_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_BODY_BYTES,
        paths=UPLOAD_PATHS,
    )

    async def consume_upload(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"size": len(body)}

    for index, path in enumerate(UPLOAD_PATHS):
        app.add_api_route(
            path,
            consume_upload,
            methods=["POST"],
            name=f"test_upload_{index}",
        )
    return app


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_upload_routes_reject_oversized_declared_content_length(path: str) -> None:
    with TestClient(_upload_app()) as client:
        response = client.post(
            path,
            content=b"x",
            headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
        )

    assert response.status_code == 413
    assert response.text == "Upload exceeds the configured size limit"


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_upload_routes_reject_oversized_stream_without_content_length(
    path: str,
) -> None:
    def chunks():
        yield b"1234"
        yield b"56789"

    with TestClient(_upload_app()) as client:
        response = client.post(path, content=chunks())

    assert response.status_code == 413
    assert response.text == "Upload exceeds the configured size limit"
