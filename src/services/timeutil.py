"""Time zones. Wall-clock times are stored naive; a meeting's `timezone` says which zone they are in."""

from datetime import datetime, timedelta, timezone as _tz
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

_VALID: Optional[set] = None


def is_valid_tz(name: Optional[str]) -> bool:
    global _VALID
    if not name or len(name) > 64:
        return False
    if _VALID is None:
        _VALID = available_timezones()
    return name in _VALID


def zone(name: Optional[str]) -> Optional[ZoneInfo]:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def to_utc(naive: datetime, tz_name: Optional[str]) -> datetime:
    """The instant (naive UTC) of a wall-clock time in `tz_name`; unchanged when no zone is known."""
    z = zone(tz_name)
    if z is None or naive is None:
        return naive
    return naive.replace(tzinfo=z).astimezone(_tz.utc).replace(tzinfo=None)


def from_utc(naive_utc: datetime, tz_name: Optional[str]) -> datetime:
    z = zone(tz_name)
    if z is None:
        return naive_utc
    return naive_utc.replace(tzinfo=_tz.utc).astimezone(z).replace(tzinfo=None)


def today_in(tz_name: Optional[str], now_utc: Optional[datetime] = None):
    """The calendar date it currently is in `tz_name` (server local date when unknown)."""
    now_utc = now_utc or datetime.utcnow()
    if zone(tz_name) is None:
        return (datetime.now() if now_utc is None else now_utc).date()
    return from_utc(now_utc, tz_name).date()


def utc_range(start: datetime, minutes: int, tz_name: Optional[str]):
    s = to_utc(start, tz_name)
    return s, s + timedelta(minutes=int(minutes or 60))
