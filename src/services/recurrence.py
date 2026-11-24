"""Dates of a repeating meeting. Times stay the same wall-clock time in the meeting's zone (so daylight saving is handled)."""

import calendar
from datetime import date, datetime, timedelta
from typing import List, Optional

RULES = ("daily", "weekdays", "weekly", "biweekly", "monthly")
LABEL = {"daily": "every day", "weekdays": "every weekday", "weekly": "every week",
         "biweekly": "every 2 weeks", "monthly": "every month"}
MAX_OCCURRENCES = 52
MAX_HORIZON_DAYS = 366 * 2


class RecurrenceError(ValueError):
    pass


def _add_months(d: datetime, months: int, day: int) -> datetime:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return d.replace(year=year, month=month, day=min(day, calendar.monthrange(year, month)[1]))


def occurrences(start: datetime, rule: str, count: Optional[int] = None, until: Optional[date] = None) -> List[datetime]:
    """The first occurrence is `start` itself; the rest follow the rule until `count` or `until`."""
    if rule not in RULES:
        raise RecurrenceError(f"Repeat must be one of: {', '.join(RULES)}")
    if count is None and until is None:
        raise RecurrenceError("Say how many times to repeat, or until when")
    if count is not None and not 2 <= count <= MAX_OCCURRENCES:
        raise RecurrenceError(f"A repeating meeting has between 2 and {MAX_OCCURRENCES} occurrences")
    if until is not None and until < start.date():
        raise RecurrenceError("The end date is before the first meeting")
    horizon = start + timedelta(days=MAX_HORIZON_DAYS)
    limit = count or MAX_OCCURRENCES
    out = [start]
    day, step = start.day, 0
    cur = start
    while len(out) < limit:
        step += 1
        if rule == "daily":
            cur = cur + timedelta(days=1)
        elif rule == "weekdays":
            cur = cur + timedelta(days=1)
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
        elif rule == "weekly":
            cur = cur + timedelta(days=7)
        elif rule == "biweekly":
            cur = cur + timedelta(days=14)
        else:
            cur = _add_months(start, step, day)
        if cur > horizon or (until is not None and cur.date() > until):
            break
        out.append(cur)
    if len(out) < 2:
        raise RecurrenceError("That period has only one occurrence; schedule it as a single meeting")
    return out
