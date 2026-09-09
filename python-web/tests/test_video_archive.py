from __future__ import annotations

import json
from pathlib import Path

import pytest

from admin_app.video_archive import (
    ProfileCapture,
    VideoArchiveError,
    VideoArchiveIdentityError,
    _declared_profile_work_count,
    _launch_persistent_context,
    _merge_records,
    _profile_listing_complete,
    _work_key_from_url,
    archive_profile_videos,
    extract_videos_from_dom,
    extract_videos_from_response,
    sync_profile_videos,
    validate_douyin_profile_url,
)


PROFILE_URL = "https://www.douyin.com/user/MS4wLjABAAAA_test-creator"
FIRST_SEEN = "2026-09-07T01:02:03Z"
LAST_SEEN = "2026-09-08T04:05:06Z"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("作品 128", 128), ("作品\n1.2万", 12_000), ("喜欢 20", None)],
)
def test_declared_profile_work_count(text: str, expected: int | None) -> None:
    assert _declared_profile_work_count(text) == expected


@pytest.mark.parametrize(
    ("declared", "captured", "explicit_empty", "expected"),
    [
        (128, 120, True, False),
        (128, 128, False, True),
        (0, 0, False, True),
        (None, 0, True, True),
        (None, 5, True, False),
    ],
)
def test_profile_listing_completion_never_lets_empty_copy_override_a_count(
    declared: int | None,
    captured: int,
    explicit_empty: bool,
    expected: bool,
) -> None:
    assert (
        _profile_listing_complete(
            declared, captured, explicit_empty=explicit_empty
        )
        is expected
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.douyin.com/video/7390123456789012345",
            "video:7390123456789012345",
        ),
        ("https://www.douyin.com/note/abc_123", "note:abc_123"),
        ("https://www.douyin.com/article/article-1", "article:article-1"),
        ("https://example.com/video/7390123456789012345", None),
    ],
)
def test_work_key_counts_non_video_cards_without_archiving_them(
    url: str, expected: str | None
) -> None:
    assert _work_key_from_url(url) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (PROFILE_URL, PROFILE_URL),
        (
            f"{PROFILE_URL}/?from_tab_name=main#ignored",
            PROFILE_URL,
        ),
        (
            "https://m.douyin.com/user/self",
            "https://m.douyin.com/user/self",
        ),
    ],
)
def test_profile_url_validation_is_strict_and_canonical(
    value: str, expected: str
) -> None:
    assert validate_douyin_profile_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://www.douyin.com/user/abc",
        "https://douyin.com.evil.example/user/abc",
        "https://douyin.com@evil.example/user/abc",
        "https://www.douyin.com/video/7390123456789012345",
        "https://www.douyin.com/user/..%2Foutside",
        "https://www.douyin.com:444/user/abc",
    ],
)
def test_profile_url_validation_rejects_non_profile_and_host_confusion(
    value: str,
) -> None:
    with pytest.raises(VideoArchiveError, match="profile_url"):
        validate_douyin_profile_url(value)


def test_response_parser_extracts_video_data_without_author_identity() -> None:
    payload = {
        "aweme_list": [
            {
                "aweme_id": "7390123456789012345",
                "item_title": "离合器保养",
                "desc": "保养步骤",
                "create_time": 1_725_667_200,
                "author": {
                    "nickname": "不得存储的昵称",
                    "uid": "private-user-id",
                    "avatar_thumb": {"url_list": ["https://example.test/a"]},
                },
                "statistics": {
                    "play_count": 1200,
                    "digg_count": 52,
                    "comment_count": 8,
                    "share_count": 3,
                    "collect_count": 12,
                },
                "video": {
                    "play_addr": {"url_list": ["https://video.example/mp4"]},
                    "cover": {
                        "url_list": [
                            "https://p3-sign.douyinpic.com/cover.jpeg?token=one"
                        ]
                    },
                },
            }
        ]
    }

    videos = extract_videos_from_response(payload, observed_at=FIRST_SEEN)

    assert videos == [
        {
            "schema_version": 1,
            "platform": "douyin",
            "video_id": "7390123456789012345",
            "url": "https://www.douyin.com/video/7390123456789012345",
            "title": "离合器保养",
            "desc": "保养步骤",
            "published_at": "2024-09-07T00:00:00Z",
            "cover_url": "https://p3-sign.douyinpic.com/cover.jpeg?token=one",
            "visible_metrics": {
                "view_count": 1200,
                "like_count": 52,
                "comment_count": 8,
                "share_count": 3,
                "collect_count": 12,
            },
            "observed_at": FIRST_SEEN,
            "sources": ["response"],
        }
    ]
    serialized = json.dumps(videos, ensure_ascii=False)
    assert "不得存储的昵称" not in serialized
    assert "private-user-id" not in serialized
    assert "play_addr" not in serialized
    assert ".mp4" not in serialized


def test_dom_parser_accepts_only_douyin_video_links_and_visible_metrics() -> None:
    videos = extract_videos_from_dom(
        [
            {
                "href": "https://www.douyin.com/video/7390123456789012345?from=profile",
                "title": "<script>不是路径</script>",
                "metric_text": "点赞 1.2万 评论: 34 播放 2亿",
            },
            "https://evil.example/video/7390123456789012346",
            "https://www.douyin.com/video/../../outside",
        ],
        observed_at=FIRST_SEEN,
    )

    assert len(videos) == 1
    assert videos[0]["video_id"] == "7390123456789012345"
    assert videos[0]["title"] == "<script>不是路径</script>"
    assert videos[0]["visible_metrics"] == {
        "view_count": 200_000_000,
        "like_count": 12_000,
        "comment_count": 34,
    }


@pytest.mark.parametrize(
    ("metric_text", "expected"),
    [
        ("评论 1,234", 1_234),
        ("评论：1.2 万", 12_000),
        ("评论数 2，345", 2_345),
    ],
)
def test_dom_comment_count_accepts_grouping_and_spaced_units(
    metric_text: str, expected: int
) -> None:
    videos = extract_videos_from_dom(
        [
            {
                "href": "https://www.douyin.com/video/7390123456789012345",
                "title": "评论格式测试",
                "metric_text": metric_text,
            }
        ],
        observed_at=FIRST_SEEN,
    )

    assert videos[0]["visible_metrics"]["comment_count"] == expected


def test_structured_comment_count_wins_over_rounded_dom_metric() -> None:
    response_record = extract_videos_from_response(
        {
            "aweme_list": [
                {
                    "aweme_id": "7390123456789012345",
                    "desc": "精确评论数",
                    "statistics_v2": {"commentCount": "11,950"},
                    "video": {},
                }
            ]
        },
        observed_at=FIRST_SEEN,
    )[0]
    dom_record = extract_videos_from_dom(
        [
            {
                "href": "https://www.douyin.com/video/7390123456789012345",
                "metric_text": "评论 1.2万",
            }
        ],
        observed_at=FIRST_SEEN,
    )[0]

    for records in ([response_record, dom_record], [dom_record, response_record]):
        merged = _merge_records(records)
        assert merged[0]["visible_metrics"]["comment_count"] == 11_950


def test_response_filter_uses_author_handle_without_persisting_it() -> None:
    def item(video_id: str, handle: str) -> dict[str, object]:
        return {
            "aweme_id": video_id,
            "desc": "主页作品",
            "author": {"unique_id": handle, "uid": f"private-{handle}"},
            "statistics": {"play_count": 1},
            "video": {},
        }

    videos = extract_videos_from_response(
        {
            "aweme_list": [
                item("7390123456789012345", "creator-handle"),
                item("7390123456789012346", "another-creator"),
            ]
        },
        observed_at=FIRST_SEEN,
        expected_handle="creator-handle",
    )

    assert [video["video_id"] for video in videos] == ["7390123456789012345"]
    serialized = json.dumps(videos)
    assert "creator-handle" not in serialized
    assert "private-" not in serialized


def _video(video_id: str, *, title: str, views: int) -> dict[str, object]:
    return {
        "video_id": video_id,
        "url": f"https://www.douyin.com/video/{video_id}",
        "title": title,
        "desc": f"{title}说明",
        "visible_metrics": {"view_count": views},
        "sources": ["response"],
    }


def test_archive_paths_and_repeated_sync_preserve_history_and_unseen_videos(
    tmp_path: Path,
) -> None:
    works = tmp_path / "works"
    first = archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=works,
        records=[
            _video("7390123456789012345", title="第一条", views=10),
            _video("7390123456789012346", title="稍后未发现", views=20),
        ],
        observed_at=FIRST_SEEN,
        scroll_count=3,
        response_count=2,
    )

    assert first.discovery_dir == works / "discovery" / "2026-09-07_01-02-03"
    assert first.profile_path.exists()
    assert first.videos_path.exists()
    assert first.created_video_ids == (
        "7390123456789012345",
        "7390123456789012346",
    )
    assert not list(works.rglob("*.mp4"))
    assert not list(works.rglob("*.tmp"))

    second = archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=works,
        records=[_video("7390123456789012345", title="第一条更新", views=25)],
        observed_at=LAST_SEEN,
    )

    manifest_path = (
        works
        / "videos"
        / "douyin"
        / "7390123456789012345"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["first_seen"] == FIRST_SEEN
    assert manifest["last_seen"] == LAST_SEEN
    assert manifest["title"] == "第一条更新"
    assert second.created_video_ids == ()
    assert second.updated_video_ids == ("7390123456789012345",)
    assert len(list((manifest_path.parent / "metadata").glob("*.json"))) == 2

    unseen_manifest = (
        works
        / "videos"
        / "douyin"
        / "7390123456789012346"
        / "manifest.json"
    )
    assert unseen_manifest.exists()
    assert json.loads(unseen_manifest.read_text(encoding="utf-8"))["last_seen"] == FIRST_SEEN

    profile = json.loads(second.profile_path.read_text(encoding="utf-8"))
    assert profile["video_count"] == 1
    lines = second.videos_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["video_id"] == "7390123456789012345"

    repeated = archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=works,
        records=[_video("7390123456789012345", title="第一条更新", views=25)],
        observed_at=LAST_SEEN,
    )
    repeated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert repeated_manifest["first_seen"] == FIRST_SEEN
    assert repeated_manifest["last_seen"] == LAST_SEEN
    assert len(list((manifest_path.parent / "metadata").glob("*.json"))) == 2
    assert repeated.updated_video_ids == ("7390123456789012345",)


def test_latest_observation_without_title_preserves_archived_title(
    tmp_path: Path,
) -> None:
    works = tmp_path / "works"
    video_id = "7390123456789012345"
    archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=works,
        records=[_video(video_id, title="已归档标题", views=10)],
        observed_at=FIRST_SEEN,
    )
    archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=works,
        records=[
            {
                **_video(video_id, title="", views=20),
                "desc": "",
            }
        ],
        observed_at=LAST_SEEN,
    )

    manifest = json.loads(
        (works / "videos" / "douyin" / video_id / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["title"] == "已归档标题"


def test_archive_rejects_malicious_video_id_before_writing(tmp_path: Path) -> None:
    with pytest.raises(VideoArchiveError, match="video_id"):
        archive_profile_videos(
            profile_url=PROFILE_URL,
            works_dir=tmp_path / "works",
            records=[{"video_id": "../../outside", "title": "bad"}],
            observed_at=FIRST_SEEN,
        )

    assert not (tmp_path / "outside").exists()


def test_cover_download_is_bounded_to_safe_raster_data_and_never_blocks_archive(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def broken_fetch(url: str) -> tuple[bytes, str]:
        calls.append(url)
        raise OSError("offline")

    result = archive_profile_videos(
        profile_url=PROFILE_URL,
        works_dir=tmp_path / "works",
        records=[
            {
                **_video("7390123456789012345", title="有封面", views=10),
                "cover_url": "https://p3-sign.douyinpic.com/cover.jpeg",
            },
            {
                **_video("7390123456789012346", title="恶意封面", views=10),
                "cover_url": "https://127.0.0.1/internal.png",
            },
        ],
        observed_at=FIRST_SEEN,
        download_covers=True,
        cover_fetcher=broken_fetch,
    )

    assert calls == ["https://p3-sign.douyinpic.com/cover.jpeg"]
    assert result.discovered_count == 2
    assert result.videos_path.exists()
    assert len(result.warnings) == 1
    assert "OSError" in result.warnings[0]


def test_sync_uses_injected_capture_and_returns_dataclass(tmp_path: Path) -> None:
    captured_arguments: dict[str, object] = {}

    def capture(**kwargs: object) -> ProfileCapture:
        captured_arguments.update(kwargs)
        return ProfileCapture(
            records=(
                _video("7390123456789012345", title="注入发现", views=10),
            ),
            scroll_count=7,
            response_count=4,
            warnings=("one recoverable warning",),
            owner_handle="creator-handle",
        )

    result = sync_profile_videos(
        profile_url=f"{PROFILE_URL}?tracking=discarded",
        works_dir=tmp_path / "works",
        observed_at=FIRST_SEEN,
        capture=capture,
        expected_handle="creator-handle",
    )

    assert result.discovered_count == 1
    assert result.warnings == ("one recoverable warning",)
    assert captured_arguments["profile_url"] == PROFILE_URL
    profile = json.loads(result.profile_path.read_text(encoding="utf-8"))
    assert profile["scroll_count"] == 7
    assert profile["response_count"] == 4
    assert "owner_handle" not in profile


def test_incomplete_profile_capture_is_preserved_as_partial_metadata(
    tmp_path: Path,
) -> None:
    def capture(**_: object) -> ProfileCapture:
        return ProfileCapture(
            records=(_video("7390123456789012345", title="部分发现", views=10),),
            owner_handle="creator-handle",
            declared_work_count=128,
            captured_work_count=120,
            listing_complete=False,
            warnings=("profile works capture was incomplete",),
        )

    result = sync_profile_videos(
        profile_url="https://www.douyin.com/user/self",
        works_dir=tmp_path / "works",
        observed_at=FIRST_SEEN,
        capture=capture,
        expected_handle="creator-handle",
    )

    assert result.discovered_count == 1
    assert result.declared_work_count == 128
    assert result.captured_work_count == 120
    assert result.listing_complete is False
    profile = json.loads(result.profile_path.read_text(encoding="utf-8"))
    assert profile["declared_work_count"] == 128
    assert profile["captured_work_count"] == 120
    assert profile["listing_complete"] is False


@pytest.mark.parametrize("owner_handle", ["", "another-creator"])
def test_sync_rejects_an_unbound_profile_before_writing(
    tmp_path: Path, owner_handle: str
) -> None:
    def capture(**_: object) -> ProfileCapture:
        return ProfileCapture(
            records=(
                _video(
                    "7390123456789012345",
                    title="身份校验视频",
                    views=1,
                ),
            ),
            owner_handle=owner_handle,
        )

    with pytest.raises(VideoArchiveIdentityError):
        sync_profile_videos(
            profile_url="https://www.douyin.com/user/self",
            works_dir=tmp_path / "works",
            observed_at=FIRST_SEEN,
            capture=capture,
            expected_handle="creator-handle",
        )

    assert not (tmp_path / "works").exists()


def test_browser_context_is_persistent_and_never_headless(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Chromium:
        def launch_persistent_context(
            self, profile: str, **options: object
        ) -> object:
            calls.append((profile, options))
            return object()

    playwright = type("Playwright", (), {"chromium": Chromium()})()

    context = _launch_persistent_context(playwright, tmp_path / "session")

    assert context is not None
    assert calls == [
        (
            str(tmp_path / "session"),
            {
                "headless": False,
                "locale": "zh-CN",
                "viewport": {"width": 1440, "height": 960},
            },
        )
    ]
