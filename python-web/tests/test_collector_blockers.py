from __future__ import annotations

from admin_app.collector import BLOCKER_SCOPE_SELECTOR, _page_blocker


class FakeScope:
    def __init__(self, text: str, *, visible: bool = True) -> None:
        self.text = text
        self.visible = visible

    def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "FakeScope":
        return self

    def is_visible(self) -> bool:
        return self.visible

    def inner_text(self, *, timeout: int) -> str:
        assert timeout > 0
        return self.text


class FakePage:
    def __init__(
        self, *, url: str, dialog_text: str = "", body_text: str = ""
    ) -> None:
        self.url = url
        self.dialog_text = dialog_text
        self.body_text = body_text

    def locator(self, selector: str) -> FakeScope:
        assert selector == BLOCKER_SCOPE_SELECTOR
        return FakeScope(self.dialog_text, visible=bool(self.dialog_text))


def test_comment_page_copy_about_login_is_not_a_blocker() -> None:
    page = FakePage(
        url="https://www.douyin.com/video/7390123456789012345",
        body_text="验证码登录教程：登录后即可查看评论",
    )

    assert _page_blocker(page) is None


def test_comment_login_dialog_is_a_blocker() -> None:
    page = FakePage(
        url="https://www.douyin.com/video/7390123456789012345",
        dialog_text="登录后即可查看评论",
    )

    assert _page_blocker(page) == (
        "The persistent browser profile is not logged in to Douyin"
    )


def test_comment_verification_url_is_authoritative() -> None:
    page = FakePage(url="https://www.douyin.com/captcha/verify")

    assert _page_blocker(page) == (
        "Douyin redirected the browser to login or verification"
    )
