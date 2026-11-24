"""
Internal scheduling logic: conflicts, free-slot search and calendar health.

Everything is computed from meetings stored in this application's database.
All datetimes are naive and share one timezone (aware inputs are converted
to UTC by `to_naive`).

Participant matching is case-insensitive. A meeting with no participants is
treated as occupying everyone (a shared calendar), so it conflicts with any
overlapping meeting.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import timeutil
from ..models.database import Meeting

INACTIVE_STATUSES = {"cancelled"}
SLOT_STEP_MINUTES = 30
BUFFER_MINUTES = 5          # gaps shorter than this count as back-to-back
MAX_SEARCH_DAYS = 60
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]


class SchedulingError(ValueError):
    """Invalid scheduling input."""


def to_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def norm_people(people: Optional[Iterable[str]]) -> set:
    return {p.strip().lower() for p in (people or []) if p and p.strip()}


def meeting_people(m: Meeting) -> set:
    """Everyone attending: listed participants plus the organizer (owner)."""
    people = norm_people(m.participants)
    owner = (getattr(m, "owner_email", None) or "").strip().lower()
    if owner:
        people.add(owner)
    return people


def advisory_lock(db: Session, name: str) -> None:
    """
    Take a transaction-scoped PostgreSQL advisory lock keyed by `name`; it is
    released at commit/rollback. Serializes concurrent requests that would
    otherwise race (double-booking, duplicate action items). No-op on other
    databases (e.g. the SQLite used in unit tests).
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
                   {"name": name})


def lock_calendar(db: Session) -> None:
    """Serialize schedule changes so concurrent requests cannot double-book."""
    advisory_lock(db, "calendar")


def meeting_end(m: Meeting) -> datetime:
    return m.scheduled_time + timedelta(minutes=int(m.duration_minutes or 60))


def _shares_people(a: set, b: set) -> bool:
    return not a or not b or bool(a & b)


def find_conflicts(
    db: Session,
    start: datetime,
    end: datetime,
    participants: Optional[Sequence[str]] = None,
    exclude_meeting_id: Optional[str] = None,
    tz: Optional[str] = None,
) -> List[Meeting]:
    """
    Active meetings that overlap [start, end) for the same people. Times are wall-clock
    in `tz`; meetings in other zones are compared by the real moment they happen.
    """
    wanted = norm_people(participants)
    # Wide enough that a zone difference (up to a day) cannot hide an overlap; exact test below.
    query = db.query(Meeting).filter(
        Meeting.scheduled_time.isnot(None),
        Meeting.scheduled_time < end + timedelta(days=1),
        Meeting.scheduled_time > start - timedelta(days=3),
    )
    if exclude_meeting_id:
        query = query.filter(Meeting.id != exclude_meeting_id)

    new_start, new_end = timeutil.to_utc(start, tz), timeutil.to_utc(end, tz)
    conflicts = []
    for m in query.all():
        if (m.status or "scheduled") in INACTIVE_STATUSES:
            continue
        # A meeting with no zone is in the same zone as the one being checked (the old behaviour).
        m_start = timeutil.to_utc(m.scheduled_time, m.timezone or tz)
        m_end = m_start + timedelta(minutes=int(m.duration_minutes or 60))
        if not (m_start < new_end and m_end > new_start):
            continue
        if _shares_people(wanted, meeting_people(m)):
            conflicts.append(m)
    conflicts.sort(key=lambda m: timeutil.to_utc(m.scheduled_time, m.timezone or tz))
    return conflicts


# ---------------------------------------------------------------------------
# Free-slot search
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    start: datetime
    end: datetime
    score: float
    reasons: List[str]


def _parse_hhmm(value: str, label: str) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        raise SchedulingError(f"{label} must be HH:MM, got {value!r}")


def parse_search_window(time_range: Optional[Dict[str, str]],
                        default_start: int, default_end: int):
    """Return (day_start, day_end, from_date, to_date) from a time_range."""
    tr = time_range or {}
    day_start = _parse_hhmm(tr["start"], "start") if "start" in tr else time(default_start)
    day_end = _parse_hhmm(tr["end"], "end") if "end" in tr else time(default_end)
    if day_start >= day_end:
        raise SchedulingError("time_range start must be before end")
    try:
        from_date = date.fromisoformat(tr["from_date"]) if "from_date" in tr else None
        to_date = date.fromisoformat(tr["to_date"]) if "to_date" in tr else None
    except ValueError:
        raise SchedulingError("from_date/to_date must be YYYY-MM-DD")
    return day_start, day_end, from_date, to_date


def suggest_slots(
    db: Session,
    participants: Sequence[str],
    duration_minutes: int,
    preferred_days: Optional[Sequence[str]] = None,
    day_start: time = time(9),
    day_end: time = time(17),
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    now: Optional[datetime] = None,
    limit: int = 5,
) -> List[Slot]:
    """Free slots for all participants, best first."""
    if not 5 <= duration_minutes <= 480:
        raise SchedulingError("duration_minutes must be between 5 and 480")

    now = now or datetime.now()
    wanted_days = set()
    for d in preferred_days or []:
        d = d.strip().lower()
        if d not in WEEKDAYS:
            raise SchedulingError(f"Unknown day {d!r}")
        wanted_days.add(WEEKDAYS.index(d))

    first_day = max(from_date or now.date(), now.date())
    last_day = to_date or (first_day + timedelta(days=13))
    if last_day < first_day:
        raise SchedulingError("to_date must not be before from_date")
    if (last_day - first_day).days > MAX_SEARCH_DAYS:
        raise SchedulingError(f"Search window is limited to {MAX_SEARCH_DAYS} days")

    window_start = datetime.combine(first_day, time.min)
    window_end = datetime.combine(last_day, time.max)
    people = norm_people(participants)

    busy = []
    for m in db.query(Meeting).filter(
        Meeting.scheduled_time.isnot(None),
        Meeting.scheduled_time <= window_end,
        Meeting.scheduled_time >= window_start - timedelta(days=2),
    ).all():
        if (m.status or "scheduled") in INACTIVE_STATUSES:
            continue
        if _shares_people(people, meeting_people(m)):
            busy.append((m.scheduled_time, meeting_end(m)))
    busy.sort()

    step = timedelta(minutes=SLOT_STEP_MINUTES)
    length = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=BUFFER_MINUTES)
    slots: List[Slot] = []

    day = first_day
    while day <= last_day:
        cursor = datetime.combine(day, day_start)
        day_limit = datetime.combine(day, day_end)
        while cursor + length <= day_limit:
            slot_end = cursor + length
            if cursor >= now and not any(
                b_start < slot_end and b_end > cursor for b_start, b_end in busy
            ):
                reasons = ["No conflicts for any participant"]
                score = 100.0

                touches = any(
                    abs((b_end - cursor).total_seconds()) < buffer.total_seconds() + 1
                    or abs((b_start - slot_end).total_seconds()) < buffer.total_seconds() + 1
                    for b_start, b_end in busy
                    if b_start.date() == day or b_end.date() == day
                )
                if touches:
                    score -= 15
                    reasons.append("Adjacent to another meeting (back-to-back)")
                else:
                    reasons.append("Buffer around the slot")

                if wanted_days:
                    if day.weekday() in wanted_days:
                        score += 10
                        reasons.append(f"Falls on a preferred day ({WEEKDAYS[day.weekday()].title()})")
                    else:
                        score -= 25
                        reasons.append("Not on a preferred day")

                hour = cursor.hour + cursor.minute / 60
                if 10 <= hour < 12:
                    score += 5
                    reasons.append("Mid-morning slot")
                elif hour >= 16:
                    score -= 5

                score -= (cursor.date() - now.date()).days * 0.5  # sooner is better
                slots.append(Slot(cursor, slot_end, round(score, 2), reasons))
            cursor += step
        day += timedelta(days=1)

    slots.sort(key=lambda s: (-s.score, s.start))
    # Keep alternatives distinct: at most one suggestion per day first.
    chosen, days_used = [], set()
    for s in slots:
        if s.start.date() not in days_used:
            chosen.append(s)
            days_used.add(s.start.date())
        if len(chosen) >= limit:
            break
    for s in slots:
        if len(chosen) >= limit:
            break
        if s not in chosen:
            chosen.append(s)
    return chosen


# ---------------------------------------------------------------------------
# Calendar health
# ---------------------------------------------------------------------------

def calendar_metrics(
    db: Session,
    user_email: str,
    days: int = 28,
    day_start: time = time(9),
    day_end: time = time(17),
    now: Optional[datetime] = None,
) -> dict:
    """
    Metrics from the user's real meetings in the last `days` days.

    A meeting belongs to the user if their email is a participant or the owner.
    - back_to_back: consecutive same-day meetings less than 5 min apart.
    - focus_time_blocks: free gaps of >= 2 hours inside working hours
      (a weekday with no meetings counts as one block).
    - back_to_back_meetings and focus_time_blocks are per-week averages.
    - fragmentation_score (0-10): average share of working-day time lost to
      gaps shorter than 2 hours between meetings, scaled to 10.
    """
    now = now or datetime.now()
    since = now - timedelta(days=days)
    email = user_email.strip().lower()
    weeks = days / 7

    meetings = [
        m for m in db.query(Meeting).filter(
            Meeting.scheduled_time.isnot(None),
            Meeting.scheduled_time >= since,
            Meeting.scheduled_time <= now,
        ).all()
        if (m.status or "scheduled") not in INACTIVE_STATUSES
        and email in meeting_people(m)
    ]
    meetings.sort(key=lambda m: m.scheduled_time)

    by_day: Dict[date, List[Meeting]] = {}
    for m in meetings:
        by_day.setdefault(m.scheduled_time.date(), []).append(m)

    total_minutes = sum(int(m.duration_minutes or 60) for m in meetings)
    back_to_back = focus_blocks = 0
    frag_ratios = []
    workday_minutes = (
        datetime.combine(date.min, day_end) - datetime.combine(date.min, day_start)
    ).total_seconds() / 60

    # Weekdays in the period with no meetings are fully free.
    day_cursor = since.date()
    while day_cursor <= now.date():
        if day_cursor.weekday() < 5 and day_cursor not in by_day:
            focus_blocks += 1
        day_cursor += timedelta(days=1)

    for day, items in by_day.items():
        edges = [datetime.combine(day, day_start)]
        fragmented = 0.0
        prev_end = edges[0]
        for m in items:
            gap = (m.scheduled_time - prev_end).total_seconds() / 60
            if prev_end != edges[0] and 0 <= gap < BUFFER_MINUTES:
                back_to_back += 1
            if gap >= 120:
                focus_blocks += 1
            elif prev_end != edges[0] and gap > 0:
                fragmented += gap
            prev_end = max(prev_end, meeting_end(m))
        tail = (datetime.combine(day, day_end) - prev_end).total_seconds() / 60
        if tail >= 120:
            focus_blocks += 1
        meeting_minutes = sum(int(m.duration_minutes or 60) for m in items)
        frag_ratios.append(
            min(1.0, (fragmented + 0.5 * meeting_minutes * (len(items) > 1))
                / workday_minutes)
        )

    return {
        "meeting_hours_per_week": round(total_minutes / 60 / weeks, 2),
        "meeting_count_per_week": round(len(meetings) / weeks),
        "average_meeting_duration": (
            round(total_minutes / len(meetings)) if meetings else 0
        ),
        "back_to_back_meetings": round(back_to_back / weeks),
        "focus_time_blocks": round(focus_blocks / weeks),
        "fragmentation_score": (
            round(10 * sum(frag_ratios) / len(frag_ratios), 1)
            if frag_ratios else 0.0
        ),
        "meetings_analyzed": len(meetings),
        "period_days": days,
    }
