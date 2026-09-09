from admin_app.local_browser import _identity_from_text


def test_identity_parser_uses_line_before_douyin_handle() -> None:
    result = _identity_from_text(
        "首页\n内容管理\n测试创作者\n抖音号：creator-handle\n粉丝\n1234",
        authorized_at="2026-09-07T00:00:00Z",
    )
    assert result is not None
    assert result.handle == "creator-handle"
    assert result.display_name == "测试创作者"


def test_identity_parser_requires_creator_handle() -> None:
    assert (
        _identity_from_text("扫码登录\n请使用抖音 App", authorized_at="now")
        is None
    )
