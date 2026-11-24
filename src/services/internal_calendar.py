"""
Internal calendar for Meeting Intelligence Agent.

Builds calendar events exclusively from data stored in this application's
PostgreSQL database: meetings (scheduled, started or recorded) and action-item deadlines. No
external calendar provider is involved.
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import timeutil
from ..models.database import ActionItemDB, Meeting

CALENDAR_ID = "internal"
CALENDAR_NAME = "Meeting Intelligence Agent"
EVENT_TYPES = {"meeting", "submission"}


class CalendarFilterError(ValueError):
    """An invalid calendar filter value was supplied."""


def parse_bound(value: Optional[str], *, end: bool) -> Optional[datetime]:
    """
    Parse a filter bound. A bare date (YYYY-MM-DD) covers the whole day, so
    as an end bound it means the last instant of that day.
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            return datetime.combine(day, time.max if end else time.min)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        raise CalendarFilterError(
            f"Invalid date {value!r}; use YYYY-MM-DD or ISO datetime"
        )


def parse_csv(value: Optional[str]) -> Optional[set]:
    if value is None or not value.strip():
        return None
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def _task_key(task: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (task or "").lower()).split())


def get_events(
    db: Session,
    *,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    event_types: Optional[Iterable[str]] = None,
    statuses: Optional[Iterable[str]] = None,
    meeting_id: Optional[str] = None,
    meeting_ids: Optional[Iterable[str]] = None,
) -> List[dict]:
    """Return all matching events, sorted chronologically."""
    if start and end and start > end:
        raise CalendarFilterError("start_date must not be after end_date")

    types = {t.lower() for t in event_types} if event_types else set(EVENT_TYPES)
    unknown = types - EVENT_TYPES
    if unknown:
        raise CalendarFilterError(
            f"Unknown event type(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(EVENT_TYPES))}"
        )
    wanted_status = {s.lower() for s in statuses} if statuses else None

    allowed = set(meeting_ids) if meeting_ids is not None else None
    if allowed is not None and not allowed:
        return []

    events: List[dict] = []

    if "meeting" in types:
        # A meeting is placed on the calendar by its scheduled time, else
        # its actual start time, else the date it was recorded/created.
        effective = func.coalesce(
            Meeting.scheduled_time, Meeting.started_at, Meeting.created_at
        )
        query = db.query(Meeting).filter(effective.isnot(None))
        if meeting_id:
            query = query.filter(Meeting.id == meeting_id)
        if allowed is not None:
            query = query.filter(Meeting.id.in_(allowed))
        if end:
            query = query.filter(effective <= end)

        for meeting in query.all():
            if meeting.scheduled_time is not None:
                begins, source = meeting.scheduled_time, "scheduled"
            elif meeting.started_at is not None:
                begins, source = meeting.started_at, "started"
            else:
                begins, source = meeting.created_at, "recorded"
            minutes = meeting.duration_minutes or 60
            try:
                finishes = begins + timedelta(minutes=int(minutes))
            except (TypeError, ValueError, OverflowError):
                finishes = begins
            status = (meeting.status or "scheduled")

            if start and finishes < start:
                continue
            if wanted_status and status.lower() not in wanted_status:
                continue

            events.append({
                "id": str(meeting.id),
                "summary": meeting.title or "Scheduled meeting",
                "start": begins.isoformat(),
                "end": finishes.isoformat(),
                "status": status,
                "type": "meeting",
                "date_source": source,
                "meeting_id": str(meeting.id),
                "duration_minutes": int(minutes),
                "timezone": meeting.timezone,
                "start_utc": (timeutil.to_utc(begins, meeting.timezone).isoformat() + "Z") if meeting.timezone else None,
                "end_utc": (timeutil.to_utc(finishes, meeting.timezone).isoformat() + "Z") if meeting.timezone else None,
                "participants": meeting.participants or [],
                "calendar_id": CALENDAR_ID,
                "calendar_name": CALENDAR_NAME,
            })

    if "submission" in types:
        query = (
            db.query(ActionItemDB, Meeting.title)
            .outerjoin(Meeting, Meeting.id == ActionItemDB.meeting_id)
            .filter(ActionItemDB.due_date.isnot(None))
        )
        if meeting_id:
            query = query.filter(ActionItemDB.meeting_id == meeting_id)
        if allowed is not None:
            query = query.filter(ActionItemDB.meeting_id.in_(allowed))
        if start:
            query = query.filter(ActionItemDB.due_date >= start)
        if end:
            query = query.filter(ActionItemDB.due_date <= end)

        seen = {}
        for item, meeting_title in query.all():
            task = (item.task or "").strip()
            if not task:
                continue
            status = item.status or "pending"
            if wanted_status and status.lower() not in wanted_status:
                continue

            # The same task of the same meeting due at the same time is one
            # event, even if it was stored more than once.
            key = (str(item.meeting_id), _task_key(task), item.due_date)
            if key in seen:
                kept = seen[key]
                if kept["status"] != "completed" and status == "completed":
                    kept.update(_submission(item, meeting_title))
                continue

            event = _submission(item, meeting_title)
            seen[key] = event
            events.append(event)

    order = {"meeting": 0, "submission": 1}
    events.sort(
        key=lambda e: (e["start"], order[e["type"]], e["summary"].lower(), e["id"])
    )
    return events


def _submission(item: ActionItemDB, meeting_title: Optional[str]) -> dict:
    due = item.due_date.isoformat()
    return {
        "id": str(item.id),
        "summary": (item.task or "").strip(),
        "start": due,
        "end": due,
        "status": item.status or "pending",
        "type": "submission",
        "meeting_id": str(item.meeting_id),
        "meeting_title": meeting_title,
        "assignee": item.assignee,
        "priority": item.priority,
        "completed_at": (
            item.completed_at.isoformat() if item.completed_at else None
        ),
        "calendar_id": CALENDAR_ID,
        "calendar_name": CALENDAR_NAME,
    }
