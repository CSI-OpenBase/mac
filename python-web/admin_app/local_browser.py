"""Interactive authorization for the local Douyin creator workspace."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .collector import _launch_persistent_context


CREATOR_HOME_URL = "https://creator.douyin.com/creator-micro/home"
HANDLE_RE = re.compile(r"抖音号\s*[：:]\s*([^\s]+)")
LOGIN_MARKERS = ("扫码登录", "验证码登录", "登录后", "请登录")
CHALLENGE_MARKERS = ("请完成验证", "检测到异常", "访问过于频繁")


class AuthorizationBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CreatorIdentity:
    handle: str
    display_name: str
    authorized_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "handle": self.handle,
            "display_name": self.display_name,
            "authorized_at": self.authorized_at,
        }


def _identity_from_text(text: str, *, authorized_at: str) -> CreatorIdentity | None:
    match = HANDLE_RE.search(text)
    if not match:
        return None
    handle = match.group(1).strip("，,;；")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    display_name = ""
    for index, line in enumerate(lines):
        if HANDLE_RE.search(line):
            if index:
                candidate = lines[index - 1]
                if candidate not in {"首页", "智能创作"} and len(candidate) <= 80:
                    display_name = candidate
            break
    return CreatorIdentity(
        handle=handle,
        display_name=display_name or handle,
        authorized_at=authorized_at,
    )


def authorize_creator(
    *,
    browser_profile_dir: Path,
    timeout_seconds: int = 300,
    expected_handle: str | None = None,
) -> CreatorIdentity:
    if not 30 <= timeout_seconds <= 900:
        raise ValueError("authorization timeout must be between 30 and 900 seconds")
    profile = browser_profile_dir.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthorizationBlocked(
            "Playwright is not installed; install the browser component first"
        ) from exc

    from datetime import datetime, timezone

    authorized_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, profile)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(CREATOR_HOME_URL, wait_until="domcontentloaded", timeout=45_000)
            deadline = time.monotonic() + timeout_seconds
            last_marker = "waiting for creator authorization"
            while time.monotonic() < deadline:
                try:
                    text = page.locator("body").inner_text(timeout=5_000)
                except Exception:
                    page.wait_for_timeout(1_000)
                    continue
                identity = _identity_from_text(text, authorized_at=authorized_at)
                if identity is not None:
                    if expected_handle and identity.handle != expected_handle:
                        raise AuthorizationBlocked(
                            "the signed-in creator does not match this workspace"
                        )
                    return identity
                if any(marker in text for marker in CHALLENGE_MARKERS):
                    last_marker = "complete the verification in the opened browser"
                elif any(marker in text for marker in LOGIN_MARKERS):
                    last_marker = "complete creator login in the opened browser"
                page.wait_for_timeout(1_000)
            raise AuthorizationBlocked(last_marker)
        finally:
            context.close()
