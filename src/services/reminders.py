"""Deadline and meeting reminders. Scanning twice never repeats a reminder (dedupe keys)."""

import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from loguru import logger
from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, User
from . import assignments, auth, notifications, timeutil

SOON_DAYS = 1                 # "due today" and "due tomorrow"
MEETING_LEAD_MINUTES = 30
USER_SCAN_EVERY = 300         # seconds between lazy scans for one person

_last_scan: Dict[str, float] = {}


def minutes_until(m: Meeting, now_utc: datetime) -> float:
    """Minutes until a scheduled meeting starts, in real time (its own zone when it has one)."""
    if m.timezone:
        return (timeutil.to_utc(m.scheduled_time, m.timezone) - now_utc).total_seconds() / 60
    local_now = now_utc + (datetime.now() - datetime.utcnow())       # naive times mean server-local time
    return (m.scheduled_time - local_now).total_seconds() / 60


def scan(db: Session, now_utc: Optional[datetime] = None, user: Optional[User] = None) -> int:
    """Create due-soon, overdue and meeting-starting reminders. Returns how many were created."""
    now_utc = now_utc or datetime.utcnow()
    meetings = [m for m in db.query(Meeting).all() if user is None or auth.can_read(user, m)]
    by_id = {m.id: m for m in meetings}
    created = 0

    # -- tasks: remind the person each one is assigned to
    tasks = (db.query(ActionItemDB)
             .filter(ActionItemDB.meeting_id.in_(list(by_id) or ["-"]), ActionItemDB.status != "completed",
                     ActionItemDB.due_date.isnot(None)).all())
    people_cache: Dict[str, list] = {}
    for a in tasks:
        m = by_id[a.meeting_id]
        if assignments.is_unclaimed(a.assignee):
            continue
        people = people_cache.setdefault(m.id, notifications.readers(db, m))
        for u in people:
            if user is not None and u.id != user.id:
                continue
            if not assignments.assignee_matches(u, a.assignee, assignments.peer_first_names(db, m, u)):
                continue
            days = (a.due_date.date() - timeutil.today_in(u.timezone, now_utc)).days
            day = a.due_date.strftime("%a %d %b")
            link = f"/meetings/{m.id}?tab=actions"
            if days < 0:
                row = notifications.notify(
                    db, u, "task_overdue", f"Overdue: {a.task}",
                    f"Due {day} in “{m.title}”. It is {-days} day{'s' if days != -1 else ''} late.", link, m.id,
                    dedupe_key=f"overdue:{a.id}:{a.due_date.date()}")
            elif days <= SOON_DAYS:
                when = "today" if days == 0 else "tomorrow"
                row = notifications.notify(
                    db, u, "deadline_soon", f"Due {when}: {a.task}", f"Due {day} in “{m.title}”.", link, m.id,
                    dedupe_key=f"due-soon:{a.id}:{a.due_date.date()}")
            else:
                continue
            created += int(row is not None)

    # -- meetings about to start
    for m in meetings:
        if m.scheduled_time is None or (m.status or "scheduled") != "scheduled":
            continue
        mins = minutes_until(m, now_utc)
        if not 0 <= mins <= MEETING_LEAD_MINUTES:
            continue
        for u in people_cache.get(m.id) or notifications.readers(db, m):
            if user is not None and u.id != user.id:
                continue
            row = notifications.notify(
                db, u, "meeting_soon", f"Starting soon: {m.title}",
                f"Starts in {max(1, round(mins))} minute{'s' if round(mins) != 1 else ''}.",
                f"/meetings/{m.id}", m.id, dedupe_key=f"meeting-soon:{m.id}:{m.scheduled_time.isoformat()}")
            created += int(row is not None)

    db.commit()
    return created


def maybe_scan_for(db: Session, user: User, now: Optional[float] = None) -> int:
    """Scan for one person, at most every few minutes (so opening the bell is always up to date)."""
    now = time.time() if now is None else now
    if now - _last_scan.get(user.id, 0) < USER_SCAN_EVERY:
        return 0
    _last_scan[user.id] = now
    try:
        return scan(db, user=user)
    except Exception:
        db.rollback()
        logger.exception("Reminder scan failed")
        return 0
