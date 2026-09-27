from datetime import datetime, timezone

from admin_app.time_utils import (
    as_beijing,
    beijing_display,
    beijing_iso,
    beijing_slug,
)


def test_beijing_helpers_convert_utc_across_midnight() -> None:
    utc_value = datetime(2026, 9, 7, 17, 2, 3, tzinfo=timezone.utc)

    localized = as_beijing(utc_value)

    assert localized is not None
    assert localized.utcoffset().total_seconds() == 8 * 60 * 60
    assert beijing_iso(utc_value) == "2026-09-08T01:02:03+08:00"
    assert beijing_display(utc_value) == "2026-09-08 01:02:03"
    assert beijing_slug(utc_value) == "2026-09-08_01-02-03"


def test_beijing_helpers_treat_naive_storage_values_as_utc() -> None:
    assert beijing_display(datetime(2026, 9, 7, 4, 0)) == "2026-09-07 12:00:00"
    assert beijing_display("2026-09-07T04:00:00Z") == "2026-09-07 12:00:00"
    assert beijing_display("not-a-time") == "—"
