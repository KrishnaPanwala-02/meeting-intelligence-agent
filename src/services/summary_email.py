"""The e-mail that shares a meeting summary and each person's own tasks."""

import os
import re
from typing import List, Optional, Tuple

from ..models.database import ActionItemDB, Meeting, Summary, User
from . import assignments
from .ask import meeting_when

MAX_RECIPIENTS = 20
MAX_NOTE = 500


def app_link(meeting_id: str) -> str:
    base = os.getenv("APP_URL", "http://localhost:5173").rstrip("/")
    return f"{base}/meetings/{meeting_id}"


def _task_line(a: ActionItemDB) -> str:
    due = a.due_date.strftime("%a %d %b %Y") if a.due_date else "no deadline"
    done = " (done)" if a.status == "completed" else ""
    who = f" — {a.assignee}" if a.assignee and a.assignee != "TBD" else ""
    return f"  • {a.task}{who}, due {due}{done}"


def plain(text: Optional[str]) -> str:
    """Single-line text: stray newlines would let stored text start new mail headers or lines."""
    return " ".join(str(text or "").split())


def compose(
    meeting: Meeting,
    summary: Summary,
    actions: List[ActionItemDB],
    recipient_email: str,
    recipient: Optional[User],
    sender_name: str,
    note: str = "",
) -> Tuple[str, str]:
    """(subject, body) for one recipient. Their own tasks come first when they have an account."""
    when = meeting_when(meeting)
    first = (recipient.name or "").split(" ")[0] if recipient and recipient.name else ""
    lines = [f"Hi{(' ' + first) if first else ''},", ""]
    lines.append(f"{plain(sender_name)} shared the summary of “{plain(meeting.title)}”"
                 + (f" ({when.strftime('%d %b %Y')})" if when else "") + ".")
    if note.strip():
        lines += ["", plain(note)[:MAX_NOTE]]
    lines += ["", "SUMMARY", plain(summary.executive_summary)]
    if summary.key_points:
        lines += ["", "KEY POINTS"]
        lines += [f"  • {plain(k.get('topic'))}: {plain(k.get('description'))}" for k in summary.key_points if isinstance(k, dict)]
    if summary.decisions:
        lines += ["", "DECISIONS"]
        for d in summary.decisions:
            if isinstance(d, dict):
                owner = plain(d.get("owner"))
                lines.append(f"  • {plain(d.get('decision'))}" + (f" ({owner})" if owner and owner != "TBD" else ""))
    if summary.next_steps:
        lines += ["", "NEXT STEPS"] + [f"  • {plain(s)}" for s in summary.next_steps]

    mine: List[ActionItemDB] = []
    if recipient is not None:
        mine = [a for a in actions if assignments.assignee_matches(recipient, a.assignee)]
    else:
        mine = [a for a in actions if a.assignee and plain(a.assignee).lower() == recipient_email.lower()]
    if mine:
        lines += ["", "YOUR TASKS"] + [_task_line(a) for a in mine]
    others = [a for a in actions if a not in mine]
    if others:
        lines += ["", "ALL OTHER TASKS" if mine else "TASKS"] + [_task_line(a) for a in others]
    lines += ["", f"Open the meeting: {app_link(meeting.id)}"]
    subject = re.sub(r"[\r\n]+", " ", f"Summary: {meeting.title}")[:150]
    return subject, "\n".join(lines) + "\n"
