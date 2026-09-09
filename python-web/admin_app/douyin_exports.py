from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence
from urllib.parse import quote, urlparse


MANIFEST_SCHEMA = "csi-openbase.douyin.creator-export-manifest"
MANIFEST_VERSION = 1
CREATOR_ORIGIN = "https://creator.douyin.com"

ExportItemStatus = Literal["succeeded", "failed", "blocked"]
ExportRunStatus = Literal["succeeded", "partial", "failed", "blocked"]
Clock = Callable[[], datetime]

_INVALID_WINDOWS_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_ALLOWED_EXPORT_EXTENSIONS = frozenset({".csv", ".xlsx"})
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_SENSITIVE_ERROR_TAIL = re.compile(
    r"(?i)\b(cookies?|authorization|password|sessions?|tokens?)\b.*$"
)

_LOGIN_TEXT = re.compile(
    r"扫码登录|手机号登录|密码登录|登录后|请先登录|重新登录|登录已失效"
)
_VERIFICATION_TEXT = re.compile(
    r"安全验证|完成验证|验证码|拖动滑块|滑块验证|访问过于频繁|异常访问"
)
_BLOCKER_SCOPE_SELECTOR = "dialog, [role='dialog'], [role='alert'], form"


class ExportConfigurationError(ValueError):
    """An export specification cannot be executed safely."""


class ExportBlockedError(RuntimeError):
    """The creator center requires an interactive operator action."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _validate_windows_filename(filename: str) -> None:
    if not filename or filename in {".", ".."}:
        raise ExportConfigurationError("ExportSpec.filename must be a file name")
    if _INVALID_WINDOWS_FILENAME.search(filename):
        raise ExportConfigurationError(
            "ExportSpec.filename must be a Windows-safe base file name"
        )
    if filename[-1] in {" ", "."}:
        raise ExportConfigurationError(
            "ExportSpec.filename cannot end with a space or period"
        )
    stem = filename.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ExportConfigurationError(
            "ExportSpec.filename cannot use a reserved Windows device name"
        )


def _validate_creator_url_template(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "creator.douyin.com":
        raise ExportConfigurationError(
            "ExportSpec.url must be an HTTPS URL on creator.douyin.com"
        )
    if re.search(
        r"(?i)(?:^|[?&])(cookie|authorization|password|session|token)=", url
    ):
        raise ExportConfigurationError(
            "ExportSpec.url cannot contain credential-like query parameters"
        )


@dataclass(frozen=True, slots=True)
class ExportSpec:
    """One creator-center export entry described by stable user-facing labels."""

    key: str
    category: str
    url: str
    button_text: str
    filename: str
    tab_text: str | None = None
    section_text: str | None = None
    button_index: int = 0
    confirm_text: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.key):
            raise ExportConfigurationError(
                "ExportSpec.key must contain lowercase letters, digits, and hyphens"
            )
        if not self.category.strip():
            raise ExportConfigurationError("ExportSpec.category cannot be empty")
        _validate_creator_url_template(self.url)
        if not self.button_text.strip():
            raise ExportConfigurationError("ExportSpec.button_text cannot be empty")
        _validate_windows_filename(self.filename)
        if self.button_index < 0:
            raise ExportConfigurationError("ExportSpec.button_index cannot be negative")

    def as_manifest_value(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "url": self.url,
            "tab_text": self.tab_text,
            "section_text": self.section_text,
            "button_text": self.button_text,
            "button_index": self.button_index,
            "confirm_text": self.confirm_text,
            "filename": self.filename,
        }


@dataclass(frozen=True, slots=True)
class ExportFileResult:
    spec: ExportSpec
    status: ExportItemStatus
    started_at: str
    finished_at: str
    file_path: Path | None = None
    size: int | None = None
    sha256: str | None = None
    error: str | None = None

    def as_manifest_value(self, run_directory: Path) -> dict[str, Any]:
        relative_file = None
        if self.file_path is not None:
            relative_file = self.file_path.relative_to(run_directory).as_posix()
        return {
            "spec": self.spec.as_manifest_value(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "file": relative_file,
            "size": self.size,
            "sha256": self.sha256,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ExportRunResult:
    run_directory: Path
    manifest_path: Path
    status: ExportRunStatus
    started_at: str
    finished_at: str
    files: tuple[ExportFileResult, ...]

    @property
    def succeeded_count(self) -> int:
        return sum(item.status == "succeeded" for item in self.files)

    @property
    def blocked_count(self) -> int:
        return sum(item.status == "blocked" for item in self.files)


_DATA_CENTER_URL = f"{CREATOR_ORIGIN}/creator-micro/data-center/operation"
_CONTENT_MANAGEMENT_URL = f"{CREATOR_ORIGIN}/creator-micro/content/manage"
_WORK_DETAIL_URL = (
    f"{CREATOR_ORIGIN}/creator-micro/work-management/work-detail/"
    "{video_id}?enter_from=homepage"
)

# This inventory mirrors the twelve export surfaces verified on 2026-09-07. Labels
# and URLs remain data so platform wording or routing changes do not require changes
# to the runner itself.
DEFAULT_EXPORT_SPECS: tuple[ExportSpec, ...] = (
    ExportSpec(
        key="data-center-posts",
        category="data-center",
        url=_DATA_CENTER_URL,
        tab_text="投稿",
        section_text="作品数据",
        button_text="导出数据",
        filename="01_data-center_posts.xlsx",
    ),
    ExportSpec(
        key="data-center-collections",
        category="data-center",
        url=_DATA_CENTER_URL,
        tab_text="合集",
        section_text="作品数据",
        button_text="导出数据",
        filename="02_data-center_collections.xlsx",
    ),
    ExportSpec(
        key="data-center-live",
        category="data-center",
        url=_DATA_CENTER_URL,
        tab_text="直播",
        section_text="作品数据",
        button_text="导出数据",
        filename="03_data-center_live.xlsx",
    ),
    ExportSpec(
        key="data-center-fans",
        category="data-center",
        url=_DATA_CENTER_URL,
        section_text="粉丝数据",
        button_text="导出数据",
        button_index=1,
        filename="04_data-center_fans.xlsx",
    ),
    ExportSpec(
        key="content-management-posts",
        category="content-management",
        url=_CONTENT_MANAGEMENT_URL,
        tab_text="作品",
        button_text="导出数据",
        filename="05_content-management_posts.xlsx",
    ),
    ExportSpec(
        key="content-management-collections",
        category="content-management",
        url=f"{_CONTENT_MANAGEMENT_URL}?tab=collections",
        tab_text="作品合集",
        button_text="导出数据",
        filename="06_content-management_collections.csv",
    ),
    ExportSpec(
        key="work-detail-overview-traffic",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="总览",
        section_text="流量",
        button_text="导出",
        filename="07_work-detail_overview-traffic.xlsx",
    ),
    ExportSpec(
        key="work-detail-overview-fans",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="总览",
        section_text="粉丝",
        button_text="导出",
        button_index=1,
        filename="08_work-detail_overview-fans.xlsx",
    ),
    ExportSpec(
        key="work-detail-content-attraction",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="流量分析",
        section_text="内容吸引力",
        button_text="导出",
        filename="09_work-detail_content-attraction.xlsx",
    ),
    ExportSpec(
        key="work-detail-audience-engagement",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="流量分析",
        section_text="观众参与度",
        button_text="导出",
        button_index=1,
        filename="10_work-detail_audience-engagement.xlsx",
    ),
    ExportSpec(
        key="work-detail-traffic-sources",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="流量分析",
        section_text="流量来源",
        button_text="导出",
        button_index=2,
        filename="11_work-detail_traffic-sources.xlsx",
    ),
    ExportSpec(
        key="work-detail-audience",
        category="work-detail",
        url=_WORK_DETAIL_URL,
        tab_text="观众分析",
        section_text="观众数据",
        button_text="导出",
        filename="12_work-detail_audience.xlsx",
    ),
)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime instances")
    return value.isoformat(timespec="seconds")


def _resolved_url(spec: ExportSpec, variables: Mapping[str, object]) -> str:
    escaped = {key: quote(str(value), safe="") for key, value in variables.items()}
    try:
        url = spec.url.format_map(escaped)
    except KeyError as exc:
        raise ExportConfigurationError(
            f"ExportSpec {spec.key!r} requires URL variable {exc.args[0]!r}"
        ) from exc
    _validate_creator_url_template(url)
    if "{" in url or "}" in url:
        raise ExportConfigurationError(
            f"ExportSpec {spec.key!r} contains an unresolved URL variable"
        )
    return url


def _create_run_directory(output_root: Path, started: datetime) -> Path:
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = started.strftime("%Y-%m-%d_%H-%M-%S")
    for collision in range(10_000):
        suffix = "" if collision == 0 else f"_{collision:02d}"
        candidate = output_root / f"{base_name}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"could not allocate an export directory under {output_root}")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    sensitive = _SENSITIVE_ERROR_TAIL.search(message)
    if sensitive:
        message = f"{message[:sensitive.start()]}sensitive-data=[redacted]"
    if not message:
        message = type(exc).__name__
    return f"{type(exc).__name__}: {message}"[:1_000]


def _is_visible(locator: Any) -> bool:
    try:
        return bool(locator.is_visible())
    except Exception:
        return False


def _visible_locator(locator: Any, index: int = 0) -> Any | None:
    try:
        count = locator.count()
    except Exception:
        return None
    visible: list[Any] = []
    for offset in range(count):
        candidate = locator.nth(offset)
        if _is_visible(candidate):
            visible.append(candidate)
    return visible[index] if index < len(visible) else None


def _named_control(
    root: Any,
    text: str | re.Pattern[str],
    *,
    index: int,
    roles: Sequence[str],
) -> Any | None:
    for role in roles:
        try:
            candidate = _visible_locator(
                root.get_by_role(role, name=text, exact=False), index
            )
        except Exception:
            candidate = None
        if candidate is not None:
            return candidate
    try:
        return _visible_locator(root.get_by_text(text, exact=False), index)
    except Exception:
        return None


def _tab_name_pattern(tab_text: str) -> re.Pattern[str]:
    escaped = re.escape(" ".join(tab_text.split()))
    return re.compile(
        rf"^{escaped}(?:\s*[（(]\s*[0-9][0-9,.]*(?:万|亿|[wW])?\s*[）)])?$"
    )


def _wait_for_value(
    page: Any,
    probe: Callable[[], Any | None],
    *,
    timeout_ms: int,
    interval_ms: int = 250,
) -> Any | None:
    attempts = max(1, (timeout_ms + interval_ms - 1) // interval_ms)
    for attempt in range(attempts):
        value = probe()
        if value is not None:
            return value
        if attempt + 1 < attempts:
            try:
                page.wait_for_timeout(interval_ms)
            except Exception:
                break
    return None


def detect_page_blocker(page: Any) -> str | None:
    """Return a stable reason without reading or persisting browser credentials."""

    try:
        current_url = str(page.url).casefold()
    except Exception:
        current_url = ""
    if "passport.douyin.com" in current_url or re.search(
        r"/(?:login|passport)(?:[/?#]|$)", current_url
    ):
        return "login_required"
    if any(marker in current_url for marker in ("captcha", "challenge", "verify")):
        return "verification_required"

    # Video titles and descriptions are rendered into the same document and can
    # legitimately discuss login or verification. Only treat those phrases as a
    # blocker inside authentication-shaped UI; URL redirects remain authoritative.
    try:
        scopes = page.locator(_BLOCKER_SCOPE_SELECTOR)
        scope_count = min(scopes.count(), 20)
    except Exception:
        scope_count = 0
    for scope_index in range(scope_count):
        scope = scopes.nth(scope_index)
        if not _is_visible(scope):
            continue
        for reason, pattern in (
            ("login_required", _LOGIN_TEXT),
            ("verification_required", _VERIFICATION_TEXT),
        ):
            try:
                matches = scope.get_by_text(pattern)
                count = min(matches.count(), 20)
            except Exception:
                continue
            if any(_is_visible(matches.nth(index)) for index in range(count)):
                return reason
    return None


def _click_named_control(
    root: Any,
    text: str,
    *,
    index: int = 0,
    timeout_ms: int = 10_000,
    roles: Sequence[str] = ("button", "tab", "link"),
) -> Any:
    candidate = _wait_for_value(
        root,
        lambda: _named_control(root, text, index=index, roles=roles),
        timeout_ms=timeout_ms,
    )
    if candidate is None:
        raise LookupError(f"could not locate visible control with text {text!r}")
    candidate.click(timeout=timeout_ms)
    return candidate


def _attribute(locator: Any, name: str) -> str | None:
    try:
        value = locator.get_attribute(name)
    except Exception:
        return None
    return str(value) if value is not None else None


def _tabpanel_text(page: Any) -> tuple[str, ...]:
    try:
        panels = page.get_by_role("tabpanel")
        count = min(panels.count(), 20)
    except Exception:
        return ()
    values: list[str] = []
    for index in range(count):
        panel = panels.nth(index)
        if not _is_visible(panel):
            continue
        try:
            values.append(" ".join(panel.inner_text().split()))
        except Exception:
            continue
    return tuple(values)


def _activate_tab(page: Any, tab_text: str, timeout_ms: int) -> None:
    # Accessible tab names are preferred; Douyin currently renders some tabs as
    # buttons, so the shared text/role fallback is intentional.
    before_panels = _tabpanel_text(page)
    tab_name = _tab_name_pattern(tab_text)
    control = _wait_for_value(
        page,
        lambda: _named_control(
            page,
            tab_name,
            index=0,
            roles=("tab", "button", "link"),
        ),
        timeout_ms=timeout_ms,
    )
    if control is None:
        raise LookupError(f"could not locate visible tab with text {tab_text!r}")
    if (_attribute(control, "aria-selected") or "").casefold() == "true":
        return
    control.click(timeout=timeout_ms)
    if not before_panels and _attribute(control, "aria-selected") is None:
        with suppress(Exception):
            page.wait_for_timeout(750)
        return

    def ready() -> bool | None:
        try:
            named_panel = page.get_by_role("tabpanel", name=tab_name, exact=False)
            if _visible_locator(named_panel) is not None:
                return True
        except Exception:
            pass
        selected = (_attribute(control, "aria-selected") or "").casefold() == "true"
        current_panels = _tabpanel_text(page)
        if selected and before_panels and current_panels != before_panels:
            return True
        return None

    ready_timeout = min(timeout_ms, 5_000)
    if _wait_for_value(page, ready, timeout_ms=ready_timeout) is None:
        # Some creator-center tabs expose neither tabpanel names nor stable text.
        # A selected tab plus a final render interval is the safest fallback.
        selected_value = _attribute(control, "aria-selected")
        if selected_value is not None and selected_value.casefold() != "true":
            raise TimeoutError(f"tab {tab_text!r} did not become selected")
        with suppress(Exception):
            page.wait_for_timeout(750)


def _section_download_control(page: Any, spec: ExportSpec) -> Any | None:
    if not spec.section_text:
        return None

    button_name_pattern = re.compile(
        rf"(?:^|\s){re.escape(spec.button_text)}\s*$"
    )

    def unique_visible_control(locator: Any) -> tuple[Any | None, bool]:
        try:
            count = min(locator.count(), 30)
        except Exception:
            return None, False
        visible: list[Any] = []
        for index in range(count):
            candidate = locator.nth(index)
            if _is_visible(candidate):
                visible.append(candidate)
                if len(visible) > 1:
                    return None, True
        return (visible[0] if visible else None), False

    for role in ("region", "group"):
        try:
            scopes = page.get_by_role(role, name=spec.section_text, exact=True)
        except Exception:
            continue
        scope = _visible_locator(scopes)
        if scope is None:
            continue
        try:
            candidate, ambiguous = unique_visible_control(
                scope.get_by_role("button", name=button_name_pattern)
            )
        except Exception:
            candidate, ambiguous = None, False
        if ambiguous:
            return None
        if candidate is not None:
            return candidate

    # When the page has no labelled region, start at an exact section heading and
    # walk outward only until its own button appears. A page-wide "following"
    # lookup can silently select the wrong table when a title or description also
    # contains a short heading such as "粉丝".
    section_pattern = re.compile(
        rf"^\s*{re.escape(spec.section_text)}"
        rf"(?:\s*{re.escape(spec.button_text)})?\s*$"
    )
    try:
        anchors = page.get_by_text(section_pattern)
        count = min(anchors.count(), 20)
    except Exception:
        return None
    for index in range(count):
        anchor = anchors.nth(index)
        if not _is_visible(anchor):
            continue
        scope = anchor
        for depth in range(7):
            try:
                candidate, ambiguous = unique_visible_control(
                    scope.get_by_role("button", name=button_name_pattern)
                )
            except Exception:
                candidate, ambiguous = None, False
            if ambiguous:
                return None
            if candidate is not None:
                return candidate
            if depth < 6:
                try:
                    scope = scope.locator("xpath=..")
                except Exception:
                    break
    return None


def _control_contains_text(locator: Any, expected: str) -> bool:
    values: list[str] = []
    for attribute in ("aria-label", "title"):
        try:
            value = locator.get_attribute(attribute)
        except Exception:
            value = None
        if value:
            values.append(str(value))
    for method_name in ("inner_text", "text_content"):
        try:
            value = getattr(locator, method_name)()
        except Exception:
            value = None
        if value:
            values.append(str(value))
    expected_normalized = " ".join(expected.split()).casefold()
    return any(
        expected_normalized in " ".join(value.split()).casefold() for value in values
    )


def _click_export_control(page: Any, spec: ExportSpec, timeout_ms: int) -> None:
    if spec.section_text:
        def section_probe() -> Any | None:
            control = _section_download_control(page, spec)
            if control is not None:
                return control
            try:
                section_pattern = re.compile(
                    rf"^\s*{re.escape(spec.section_text)}"
                    rf"(?:\s*{re.escape(spec.button_text)})?\s*$"
                )
                section = _visible_locator(
                    page.get_by_text(section_pattern)
                )
            except Exception:
                section = None
            return False if section is not None else None

        section_control = _wait_for_value(
            page,
            section_probe,
            timeout_ms=timeout_ms,
        )
        if section_control is not None and section_control is not False:
            section_control.click(timeout=timeout_ms)
            return
    _click_named_control(
        page,
        spec.button_text,
        index=spec.button_index,
        timeout_ms=timeout_ms,
    )


def _download_extension(download: Any, configured_filename: str) -> str:
    try:
        suggested = str(download.suggested_filename or "")
    except Exception:
        suggested = ""
    # Only the final extension is accepted from the remote suggestion. The path and
    # base name always come from the validated local ExportSpec.
    suggested_leaf = suggested.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(r"(\.[A-Za-z0-9]{1,16})$", suggested_leaf)
    if match and match.group(1).casefold() in _ALLOWED_EXPORT_EXTENSIONS:
        return match.group(1)
    configured_match = re.search(r"(\.[A-Za-z0-9]{1,16})$", configured_filename)
    if configured_match and configured_match.group(1).casefold() in _ALLOWED_EXPORT_EXTENSIONS:
        return configured_match.group(1)
    raise ExportConfigurationError("export files must use .xlsx or .csv")


def _destination_for(run_directory: Path, spec: ExportSpec, download: Any) -> Path:
    configured_suffix = Path(spec.filename).suffix
    stem = spec.filename[: -len(configured_suffix)] if configured_suffix else spec.filename
    extension = _download_extension(download, spec.filename)
    filename = f"{stem}{extension}"
    _validate_windows_filename(filename)
    candidate = run_directory / filename
    collision = 1
    while candidate.exists():
        collision += 1
        candidate = run_directory / f"{stem}_{collision:02d}{extension}"
    if candidate.resolve().parent != run_directory.resolve():
        raise ExportConfigurationError("download destination escaped the export directory")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_one_export(
    page: Any,
    run_directory: Path,
    spec: ExportSpec,
    variables: Mapping[str, object],
    clock: Clock,
    navigation_timeout_ms: int,
    download_timeout_ms: int,
) -> ExportFileResult:
    started_at = _timestamp(clock())
    try:
        url = _resolved_url(spec, variables)
        page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        blocker = detect_page_blocker(page)
        if blocker:
            raise ExportBlockedError(blocker)
        if spec.tab_text:
            _activate_tab(page, spec.tab_text, navigation_timeout_ms)
            blocker = detect_page_blocker(page)
            if blocker:
                raise ExportBlockedError(blocker)

        with page.expect_download(timeout=download_timeout_ms) as download_info:
            _click_export_control(page, spec, navigation_timeout_ms)
            if spec.confirm_text:
                _click_named_control(
                    page, spec.confirm_text, timeout_ms=navigation_timeout_ms
                )
        download = download_info.value
        failure_method = getattr(download, "failure", None)
        failure = failure_method() if callable(failure_method) else None
        if failure:
            raise RuntimeError(f"browser download failed: {failure}")
        destination = _destination_for(run_directory, spec, download)
        download.save_as(str(destination))
        if not destination.is_file():
            raise OSError("browser reported a download but did not create a file")
        size = destination.stat().st_size
        if size == 0:
            destination.unlink(missing_ok=True)
            raise OSError("browser downloaded an empty export file")
        return ExportFileResult(
            spec=spec,
            status="succeeded",
            started_at=started_at,
            finished_at=_timestamp(clock()),
            file_path=destination,
            size=size,
            sha256=_sha256_file(destination),
        )
    except ExportBlockedError as exc:
        return ExportFileResult(
            spec=spec,
            status="blocked",
            started_at=started_at,
            finished_at=_timestamp(clock()),
            error=exc.reason,
        )
    except Exception as exc:
        return ExportFileResult(
            spec=spec,
            status="failed",
            started_at=started_at,
            finished_at=_timestamp(clock()),
            error=_safe_error(exc),
        )


def launch_persistent_context(playwright: Any, profile_directory: Path) -> Any:
    """Launch a visible persistent browser, preferring Playwright Chromium."""

    profile_directory = profile_directory.expanduser().resolve()
    profile_directory.mkdir(parents=True, exist_ok=True)
    options = {
        "headless": False,
        "accept_downloads": True,
        "locale": "zh-CN",
        "viewport": {"width": 1440, "height": 960},
    }
    failures: list[str] = []
    for channel in (None, "chrome", "msedge"):
        try:
            if channel is None:
                return playwright.chromium.launch_persistent_context(
                    str(profile_directory), **options
                )
            return playwright.chromium.launch_persistent_context(
                str(profile_directory), channel=channel, **options
            )
        except Exception as exc:
            failures.append(f"{channel or 'playwright'}: {type(exc).__name__}")
    raise RuntimeError(
        "No compatible Chromium browser could be launched ("
        + ", ".join(failures)
        + "); run `python -m playwright install chromium`"
    )


def _page_from_context(context: Any) -> Any:
    pages = context.pages
    return pages[0] if pages else context.new_page()


@contextmanager
def _owned_browser_page(profile_directory: Path) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed; install it and a compatible Chromium browser"
        ) from exc

    manager = sync_playwright()
    playwright = manager.start()
    context = None
    try:
        context = launch_persistent_context(playwright, profile_directory)
        yield _page_from_context(context)
    finally:
        if context is not None:
            with suppress(Exception):
                context.close()
        with suppress(Exception):
            playwright.stop()


def _run_status(files: Sequence[ExportFileResult]) -> ExportRunStatus:
    if any(item.status == "blocked" for item in files):
        return "blocked"
    if all(item.status == "succeeded" for item in files):
        return "succeeded"
    if any(item.status == "succeeded" for item in files):
        return "partial"
    return "failed"


def _manifest(
    *,
    started_at: str,
    finished_at: str | None,
    status: str,
    run_directory: Path,
    files: Sequence[ExportFileResult],
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "version": MANIFEST_VERSION,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "exports": [item.as_manifest_value(run_directory) for item in files],
    }


def export_creator_data(
    output_root: Path,
    *,
    browser_profile_dir: Path | None = None,
    specs: Sequence[ExportSpec] = DEFAULT_EXPORT_SPECS,
    variables: Mapping[str, object] | None = None,
    page: Any | None = None,
    context: Any | None = None,
    clock: Clock = _local_now,
    navigation_timeout_ms: int = 45_000,
    download_timeout_ms: int = 120_000,
) -> ExportRunResult:
    """Export configured creator-center tables into one auditable run directory.

    ``page`` and ``context`` are caller-owned injection points. When neither is
    supplied, the function owns a visible persistent Playwright context. A failed
    entry never prevents later entries from being attempted.
    """

    if navigation_timeout_ms <= 0 or download_timeout_ms <= 0:
        raise ValueError("browser timeouts must be positive")
    selected_specs = tuple(specs)
    if len({spec.key for spec in selected_specs}) != len(selected_specs):
        raise ExportConfigurationError("ExportSpec keys must be unique within a run")
    started = clock()
    started_at = _timestamp(started)
    run_directory = _create_run_directory(Path(output_root), started)
    manifest_path = run_directory / "manifest.json"
    files: list[ExportFileResult] = []
    _atomic_write_json(
        manifest_path,
        _manifest(
            started_at=started_at,
            finished_at=None,
            status="running",
            run_directory=run_directory,
            files=files,
        ),
    )

    def execute(active_page: Any) -> None:
        for spec in selected_specs:
            files.append(
                _run_one_export(
                    active_page,
                    run_directory,
                    spec,
                    variables or {},
                    clock,
                    navigation_timeout_ms,
                    download_timeout_ms,
                )
            )
            _atomic_write_json(
                manifest_path,
                _manifest(
                    started_at=started_at,
                    finished_at=None,
                    status="running",
                    run_directory=run_directory,
                    files=files,
                ),
            )

    try:
        if page is not None:
            execute(page)
        elif context is not None:
            execute(_page_from_context(context))
        else:
            profile = (
                Path(browser_profile_dir)
                if browser_profile_dir is not None
                else Path(output_root).expanduser().resolve().parent / "browser-profile"
            )
            with _owned_browser_page(profile) as owned_page:
                execute(owned_page)
    except Exception as exc:
        # Browser startup and unexpected context-level failures are represented for
        # every unattempted entry, preserving the one-result-per-spec contract.
        attempted = {item.spec.key for item in files}
        for spec in selected_specs:
            if spec.key in attempted:
                continue
            moment = _timestamp(clock())
            files.append(
                ExportFileResult(
                    spec=spec,
                    status="failed",
                    started_at=moment,
                    finished_at=moment,
                    error=_safe_error(exc),
                )
            )

    finished_at = _timestamp(clock())
    status = _run_status(files)
    _atomic_write_json(
        manifest_path,
        _manifest(
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            run_directory=run_directory,
            files=files,
        ),
    )
    return ExportRunResult(
        run_directory=run_directory,
        manifest_path=manifest_path,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        files=tuple(files),
    )


# A short alias keeps call sites readable while retaining the domain-specific name.
run_exports = export_creator_data


__all__ = [
    "DEFAULT_EXPORT_SPECS",
    "ExportConfigurationError",
    "ExportFileResult",
    "ExportRunResult",
    "ExportSpec",
    "detect_page_blocker",
    "export_creator_data",
    "launch_persistent_context",
    "run_exports",
]
