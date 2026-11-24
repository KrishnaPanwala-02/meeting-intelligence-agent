"""
iCalendar (.ics) export, so meetings and task deadlines can be added to Google
Calendar, Apple Calendar or Outlook without any API or account link.

Times in this app are naive wall-clock values, so events use "floating" times
(no time zone): they appear at the same clock time wherever the calendar is read.
Task deadlines are all-day events, which every calendar app accepts (not every one
imports the VTODO to-do type).
"""

import re
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from ..models.database import ActionItemDB, Meeting
from . import scheduling, timeutil

PRODID = "-//Meeting Intelligence Agent//EN"
UID_DOMAIN = "meeting-intelligence.local"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def escape(text: Optional[str]) -> str:
    """Escape a TEXT value (RFC 5545 §3.3.11) and drop control characters."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _CONTROL.sub(" ", s)
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\n", "\\n")


def fold(line: str) -> str:
    """Lines may not exceed 75 octets: continue on the next line after CRLF + space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current, size = [], "", 0
    limit = 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append(current)
            current, size, limit = ch, n, 74          # continuation lines start with a space
        else:
            current += ch
            size += n
    parts.append(current)
    return "\r\n ".join(parts)


def _floating(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _utc(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def meeting_event(m: Meeting, now: Optional[datetime] = None, link: Optional[str] = None) -> List[str]:
    """A VEVENT for a meeting: at its scheduled time, else at the moment it was recorded."""
    now = now or datetime.utcnow()
    people = [str(p) for p in (m.participants or [])]
    lines = ["BEGIN:VEVENT", f"UID:meeting-{m.id}@{UID_DOMAIN}", f"DTSTAMP:{_utc(now)}"]
    if m.scheduled_time is not None and m.timezone:
        start_utc = timeutil.to_utc(m.scheduled_time, m.timezone)
        end_utc = timeutil.to_utc(scheduling.meeting_end(m), m.timezone)
        lines += [f"DTSTART:{_utc(start_utc)}", f"DTEND:{_utc(end_utc)}"]
    elif m.scheduled_time is not None:
        lines += [f"DTSTART:{_floating(m.scheduled_time)}", f"DTEND:{_floating(scheduling.meeting_end(m))}"]
    else:                                              # recorded: created_at is stored in UTC
        start = m.started_at or m.created_at or now
        minutes = int(m.duration_minutes or 60)
        lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(start + timedelta(minutes=minutes))}"]
    lines.append(f"SUMMARY:{escape(m.title)}")
    desc = []
    if m.owner_email:
        desc.append(f"Organiser: {m.owner_email}")
    if people:
        desc.append("Participants: " + ", ".join(people))
    if link:
        desc.append(f"Open: {link}")
    if desc:
        lines.append("DESCRIPTION:" + escape("\n".join(desc)))
    lines.append("STATUS:CANCELLED" if m.status == "cancelled" else "STATUS:CONFIRMED")
    lines.append("END:VEVENT")
    return lines


_PRIORITY = {"high": 1, "medium": 5, "low": 9}


def task_event(a: ActionItemDB, meeting_title: str, now: Optional[datetime] = None) -> List[str]:
    """An all-day event on the deadline of a task (the task must have a due date)."""
    now = now or datetime.utcnow()
    day = a.due_date
    who = a.assignee if a.assignee and a.assignee != "TBD" else None
    desc = [f"Meeting: {meeting_title}"] + ([f"Assigned to: {who}"] if who else []) + [f"Status: {a.status}"]
    return [
        "BEGIN:VEVENT",
        f"UID:task-{a.id}@{UID_DOMAIN}",
        f"DTSTAMP:{_utc(now)}",
        f"DTSTART;VALUE=DATE:{_date(day)}",
        f"DTEND;VALUE=DATE:{_date(day + timedelta(days=1))}",
        f"SUMMARY:{escape('Task due: ' + (a.task or ''))}",
        "DESCRIPTION:" + escape("\n".join(desc)),
        "CATEGORIES:TASK",
        f"PRIORITY:{_PRIORITY.get(a.priority or 'medium', 5)}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def build_calendar(events: Iterable[List[str]], name: str = "Meeting Intelligence") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{escape(name)}"]
    for ev in events:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"
