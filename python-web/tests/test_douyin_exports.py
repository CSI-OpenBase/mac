from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

import pytest

from admin_app.douyin_exports import (
    DEFAULT_EXPORT_SPECS,
    ExportConfigurationError,
    ExportSpec,
    detect_page_blocker,
    export_creator_data,
    launch_persistent_context,
    _click_export_control,
    _tab_name_pattern,
)


FIXED_NOW = datetime(2026, 9, 7, 15, 30, 25, tzinfo=timezone.utc)


class FakeControl:
    def __init__(
        self,
        *,
        visible: bool = True,
        click: Callable[[], None] | None = None,
        text: str = "",
    ) -> None:
        self.visible = visible
        self._click = click or (lambda: None)
        self.text = text


class FakeLocator:
    def __init__(self, controls: list[FakeControl] | None = None) -> None:
        self.controls = controls or []

    def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> "FakeLocator":
        if not 0 <= index < len(self.controls):
            return FakeLocator()
        return FakeLocator([self.controls[index]])

    def is_visible(self) -> bool:
        return bool(self.controls and self.controls[0].visible)

    def click(self, *, timeout: int) -> None:
        assert timeout > 0
        if not self.controls:
            raise RuntimeError("missing control")
        self.controls[0]._click()

    def locator(self, selector: str) -> "FakeLocator":
        assert selector.startswith("xpath=")
        return FakeLocator()

    def get_by_text(
        self, value: str | re.Pattern[str], *, exact: bool = False
    ) -> "FakeLocator":
        controls: list[FakeControl] = []
        for control in self.controls:
            matched = (
                value.search(control.text)
                if isinstance(value, re.Pattern)
                else (control.text == value if exact else value in control.text)
            )
            if matched:
                controls.append(control)
        return FakeLocator(controls)


class FakeDownload:
    def __init__(self, suggested_filename: str, content: bytes) -> None:
        self.suggested_filename = suggested_filename
        self.content = content
        self.saved_to: Path | None = None

    def failure(self) -> None:
        return None

    def save_as(self, path: str) -> None:
        self.saved_to = Path(path)
        self.saved_to.write_bytes(self.content)


class FakeDownloadInfo:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.download: FakeDownload | None = None

    def __enter__(self) -> "FakeDownloadInfo":
        self.page.active_download = self
        return self

    def __exit__(self, *_args: object) -> None:
        self.page.active_download = None

    @property
    def value(self) -> FakeDownload:
        if self.download is None:
            raise RuntimeError("the fake page did not produce a download")
        return self.download


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.visited: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.redirects: dict[str, str] = {}
        self.downloads: dict[str, FakeDownload] = {}
        self.visible_text: list[str] = []
        self.semantic_messages: list[str] = []
        self.active_download: FakeDownloadInfo | None = None
        self.waits: list[int] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout > 0
        self.visited.append(url)
        if url in self.failures:
            raise self.failures[url]
        self.url = self.redirects.get(url, url)

    def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)

    def expect_download(self, *, timeout: int) -> FakeDownloadInfo:
        assert timeout > 0
        return FakeDownloadInfo(self)

    def _click(self) -> None:
        if self.active_download is None:
            return
        download = self.downloads.get(self.url)
        if download is not None:
            self.active_download.download = download

    def get_by_role(
        self, role: str, *, name: str, exact: bool = False
    ) -> FakeLocator:
        assert exact is False
        if role in {"button", "tab", "link"}:
            return FakeLocator([FakeControl(click=self._click, text=name)])
        return FakeLocator()

    def get_by_text(self, value: str | re.Pattern[str], *, exact: bool = False) -> FakeLocator:
        controls: list[FakeControl] = []
        for text in self.visible_text:
            matched = value.search(text) if isinstance(value, re.Pattern) else value in text
            if matched:
                controls.append(FakeControl(text=text))
        return FakeLocator(controls)

    def locator(self, selector: str) -> FakeLocator:
        assert selector == "dialog, [role='dialog'], [role='alert'], form"
        return FakeLocator(
            [FakeControl(text=message) for message in self.semantic_messages]
        )


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.new_page_called = False

    def new_page(self) -> FakePage:
        self.new_page_called = True
        return self.pages[0]


def spec(key: str, path: str, filename: str = "table.xlsx") -> ExportSpec:
    return ExportSpec(
        key=key,
        category="test",
        url=f"https://creator.douyin.com/{path}",
        button_text="导出",
        filename=filename,
    )


def test_default_inventory_matches_twelve_verified_exports() -> None:
    assert len(DEFAULT_EXPORT_SPECS) == 12
    assert [item.filename for item in DEFAULT_EXPORT_SPECS] == [
        "01_data-center_posts.xlsx",
        "02_data-center_collections.xlsx",
        "03_data-center_live.xlsx",
        "04_data-center_fans.xlsx",
        "05_content-management_posts.xlsx",
        "06_content-management_collections.csv",
        "07_work-detail_overview-traffic.xlsx",
        "08_work-detail_overview-fans.xlsx",
        "09_work-detail_content-attraction.xlsx",
        "10_work-detail_audience-engagement.xlsx",
        "11_work-detail_traffic-sources.xlsx",
        "12_work-detail_audience.xlsx",
    ]
    assert all(item.button_text for item in DEFAULT_EXPORT_SPECS)
    assert all(item.category for item in DEFAULT_EXPORT_SPECS)
    assert "{video_id}" in DEFAULT_EXPORT_SPECS[6].url


def test_content_management_tab_name_does_not_match_publish_or_collection() -> None:
    pattern = _tab_name_pattern("作品")
    assert pattern.fullmatch("作品")
    assert pattern.fullmatch("作品 (142)")
    assert pattern.fullmatch("作品（1.2万）")
    assert not pattern.fullmatch("作品发布")
    assert not pattern.fullmatch("作品合集 (4)")


@pytest.mark.parametrize(
    "filename",
    ["../escape.xlsx", "folder/file.xlsx", "folder\\file.xlsx", "CON.csv", "bad. "],
)
def test_export_spec_rejects_unsafe_windows_filenames(filename: str) -> None:
    with pytest.raises(ExportConfigurationError):
        spec("unsafe-name", "safe", filename)


def test_export_spec_rejects_credentials_in_manifest_url() -> None:
    with pytest.raises(ExportConfigurationError):
        ExportSpec(
            key="unsafe-url",
            category="test",
            url="https://creator.douyin.com/export?cookie=secret",
            button_text="导出",
            filename="table.xlsx",
        )


def test_failed_entry_does_not_prevent_later_download_and_manifest_is_complete(
    tmp_path: Path,
) -> None:
    first = spec("first", "first")
    second = spec("second", "second", "local-name.xlsx")
    page = FakePage()
    page.failures[first.url] = RuntimeError(
        "request failed token=do-not-store cookie=session-secret"
    )
    content = b"platform export bytes"
    page.downloads[second.url] = FakeDownload("../../platform-export.CSV", content)

    result = export_creator_data(
        tmp_path,
        specs=(first, second),
        page=page,
        clock=lambda: FIXED_NOW,
    )

    assert result.status == "partial"
    assert result.run_directory.name == "2026-09-07_15-30-25"
    assert page.visited == [first.url, second.url]
    assert [item.status for item in result.files] == ["failed", "succeeded"]
    exported = result.files[1]
    assert exported.file_path == result.run_directory / "local-name.CSV"
    assert exported.size == len(content)
    assert exported.sha256 == hashlib.sha256(content).hexdigest()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema"] == "csi-openbase.douyin.creator-export-manifest"
    assert manifest["version"] == 1
    assert manifest["started_at"] == "2026-09-07T15:30:25+00:00"
    assert manifest["finished_at"] == "2026-09-07T15:30:25+00:00"
    assert manifest["exports"][1]["file"] == "local-name.CSV"
    assert manifest["exports"][1]["size"] == len(content)
    assert manifest["exports"][1]["sha256"] == exported.sha256
    serialized = json.dumps(manifest)
    assert "do-not-store" not in serialized
    assert "session-secret" not in serialized
    assert not list(result.run_directory.glob(".manifest.json.*.tmp"))


def test_export_waits_for_a_control_rendered_after_dom_content_loaded(
    tmp_path: Path,
) -> None:
    class SlowPage(FakePage):
        def get_by_role(
            self, role: str, *, name: str, exact: bool = False
        ) -> FakeLocator:
            if len(self.waits) < 2:
                return FakeLocator()
            return super().get_by_role(role, name=name, exact=exact)

    item = spec("slow-render", "slow")
    page = SlowPage()
    page.downloads[item.url] = FakeDownload("slow.xlsx", b"ready")

    result = export_creator_data(
        tmp_path,
        specs=(item,),
        page=page,
        clock=lambda: FIXED_NOW,
        navigation_timeout_ms=2_000,
    )

    assert result.status == "succeeded"
    assert page.waits[:2] == [250, 250]


def test_remote_filename_cannot_change_a_table_export_to_executable(
    tmp_path: Path,
) -> None:
    item = spec("extension", "extension", "safe-name.xlsx")
    page = FakePage()
    page.downloads[item.url] = FakeDownload("untrusted.exe", b"table data")

    result = export_creator_data(
        tmp_path,
        specs=(item,),
        page=page,
        clock=lambda: FIXED_NOW,
    )

    assert result.status == "succeeded"
    assert result.files[0].file_path.name == "safe-name.xlsx"
    assert not list(result.run_directory.glob("*.exe"))


def test_empty_download_is_failed_and_removed(tmp_path: Path) -> None:
    item = spec("empty", "empty")
    page = FakePage()
    page.downloads[item.url] = FakeDownload("empty.xlsx", b"")

    result = export_creator_data(
        tmp_path,
        specs=(item,),
        page=page,
        clock=lambda: FIXED_NOW,
    )

    assert result.status == "failed"
    assert result.files[0].status == "failed"
    assert not list(result.run_directory.glob("*.xlsx"))


def test_login_redirect_is_blocked_and_next_entry_is_still_attempted(
    tmp_path: Path,
) -> None:
    first = spec("needs-login", "needs-login")
    second = spec("available", "available")
    page = FakePage()
    page.redirects[first.url] = "https://passport.douyin.com/login/"
    page.downloads[second.url] = FakeDownload("result.xlsx", b"ok")

    result = export_creator_data(
        tmp_path,
        specs=(first, second),
        page=page,
        clock=lambda: FIXED_NOW,
    )

    assert result.status == "blocked"
    assert [item.status for item in result.files] == ["blocked", "succeeded"]
    assert result.files[0].error == "login_required"
    assert page.visited == [first.url, second.url]


def test_visible_verification_message_is_detected() -> None:
    page = FakePage()
    page.url = "https://creator.douyin.com/creator-micro/data-center/operation"
    page.semantic_messages = ["请完成安全验证"]

    assert detect_page_blocker(page) == "verification_required"


def test_video_copy_about_login_does_not_trigger_a_page_blocker() -> None:
    page = FakePage()
    page.url = "https://creator.douyin.com/creator-micro/work-management/work-detail/123"
    page.visible_text = ["验证码登录教程：登录后如何保护账号"]

    assert detect_page_blocker(page) is None


def test_run_directory_is_collision_safe_with_an_injected_clock(
    tmp_path: Path,
) -> None:
    page = FakePage()

    first = export_creator_data(tmp_path, specs=(), page=page, clock=lambda: FIXED_NOW)
    second = export_creator_data(tmp_path, specs=(), page=page, clock=lambda: FIXED_NOW)

    assert first.run_directory.name == "2026-09-07_15-30-25"
    assert second.run_directory.name == "2026-09-07_15-30-25_01"
    assert first.status == second.status == "succeeded"


def test_context_and_url_variables_are_injectable(tmp_path: Path) -> None:
    detail = ExportSpec(
        key="detail",
        category="work-detail",
        url="https://creator.douyin.com/work/{video_id}",
        tab_text="总览",
        section_text="流量",
        button_text="导出",
        filename="detail.xlsx",
    )
    page = FakePage()
    resolved = "https://creator.douyin.com/work/1234567890123456789"
    page.downloads[resolved] = FakeDownload("douyin.xlsx", b"detail")
    context = FakeContext(page)

    result = export_creator_data(
        tmp_path,
        specs=(detail,),
        variables={"video_id": "1234567890123456789"},
        context=context,
        clock=lambda: FIXED_NOW,
    )

    assert result.status == "succeeded"
    assert page.visited == [resolved]
    assert context.new_page_called is False


def test_section_export_uses_exact_heading_and_its_own_button() -> None:
    clicked: list[str] = []

    class SectionScope:
        def __init__(self, button_name: str | None = None) -> None:
            self.button_name = button_name

        def count(self) -> int:
            return 1

        def nth(self, _index: int) -> "SectionScope":
            return self

        def is_visible(self) -> bool:
            return True

        def locator(self, selector: str) -> "SectionScope":
            assert selector == "xpath=.."
            return SectionScope("fans")

        def get_by_role(
            self, role: str, *, name: str | re.Pattern[str], exact: bool = False
        ) -> FakeLocator:
            assert role == "button"
            assert isinstance(name, re.Pattern)
            assert name.search("download_stroked 导出")
            assert not name.search("导出其他数据")
            assert exact is False
            if self.button_name is None:
                return FakeLocator()
            return FakeLocator(
                [FakeControl(click=lambda: clicked.append(self.button_name or ""))]
            )

    class AmbiguousSectionPage(FakePage):
        def get_by_role(
            self, role: str, *, name: str, exact: bool = False
        ) -> FakeLocator:
            if role in {"region", "group"}:
                assert exact is True
                return FakeLocator()
            return super().get_by_role(role, name=name, exact=exact)

        def get_by_text(
            self, value: str | re.Pattern[str], *, exact: bool = False
        ) -> SectionScope:
            assert isinstance(value, re.Pattern)
            assert value.fullmatch("粉丝")
            assert value.fullmatch("粉丝\n导出")
            assert not value.fullmatch("视频描述里提到粉丝")
            assert exact is False
            return SectionScope()

    item = ExportSpec(
        key="fans",
        category="work-detail",
        url="https://creator.douyin.com/work/1234567890123456789",
        section_text="粉丝",
        button_text="导出",
        button_index=1,
        filename="fans.xlsx",
    )

    _click_export_control(AmbiguousSectionPage(), item, timeout_ms=2_000)

    assert clicked == ["fans"]


class FakeChromium:
    def __init__(self) -> None:
        self.channels: list[str | None] = []
        self.context = object()

    def launch_persistent_context(self, profile: str, **options: Any) -> object:
        assert Path(profile).name == "profile"
        assert options["headless"] is False
        assert options["accept_downloads"] is True
        self.channels.append(options.get("channel"))
        if options.get("channel") is None:
            raise RuntimeError("bundled browser unavailable")
        return self.context


def test_browser_launch_is_headed_persistent_and_falls_back_to_channels(
    tmp_path: Path,
) -> None:
    chromium = FakeChromium()
    playwright = type("FakePlaywright", (), {"chromium": chromium})()

    context = launch_persistent_context(playwright, tmp_path / "profile")

    assert context is chromium.context
    assert chromium.channels == [None, "chrome"]
