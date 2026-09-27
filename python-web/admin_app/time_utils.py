"""Beijing-time presentation helpers with UTC-safe parsing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


BEIJING_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")


def as_beijing(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        value = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime, ISO string, or null")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TIMEZONE)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


def beijing_iso(value: datetime | str | None) -> str:
    localized = as_beijing(value)
    return localized.isoformat(timespec="seconds") if localized else ""


def beijing_display(
    value: datetime | str | None,
    *,
    fallback: str = "—",
    timespec: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    try:
        localized = as_beijing(value)
    except (TypeError, ValueError):
        return fallback
    return localized.strftime(timespec) if localized else fallback


def beijing_slug(value: datetime | str | None = None) -> str:
    localized = as_beijing(value) if value is not None else beijing_now()
    assert localized is not None
    return localized.strftime("%Y-%m-%d_%H-%M-%S")
