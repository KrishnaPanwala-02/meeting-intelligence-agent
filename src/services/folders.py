"""
Folders: a named group of one person's meetings (a project, a recurring series) that remember each other.

Meetings in a folder are numbered by date (Meeting 1, 2, 3...). When a later meeting is processed, what the
earlier ones decided and left open is handed to the summary step, so the new summary can say how it
continues them. Only the owner has folders; a folder never changes what a meeting's own summary says.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Folder, Meeting, Summary, User

MAX_NAME_LENGTH = 80
MEMORY_MEETINGS = 5            # how many earlier meetings are read, newest first
MEMORY_OPEN_TASKS = 12
SUMMARY_CHARS = 450
MEMORY_MAX_CHARS = 4000


class FolderError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def key(name: Optional[str]) -> str:
    return " ".join((name or "").split()).casefold()


def clean_name(name: Optional[str]) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise FolderError("Give the folder a name", 422)
    if len(cleaned) > MAX_NAME_LENGTH:
        raise FolderError(f"A folder name can be at most {MAX_NAME_LENGTH} characters", 422)
    if any(not ch.isprintable() for ch in cleaned):
        raise FolderError("The folder name contains invalid characters", 422)
    return cleaned


def get_owned(db: Session, user: User, folder_id: Optional[str]) -> Folder:
    """The user's own folder, or a 404 (folders are private: someone else's looks the same as none)."""
    row = db.get(Folder, folder_id) if folder_id else None
    if row is None or row.owner_id != user.id:
        raise FolderError("Folder not found", 404)
    return row


def create(db: Session, user: User, name: str) -> Folder:
    name = clean_name(name)
    if db.query(Folder).filter(Folder.owner_id == user.id, Folder.name_key == key(name)).first():
        raise FolderError("You already have a folder with that name", 409)
    row = Folder(id=str(uuid.uuid4()), owner_id=user.id, name=name, name_key=key(name),
                 created_at=datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def get_or_create(db: Session, user: User, name: str) -> Folder:
    """The user's folder with this name (a different capitalisation is the same folder), made if new."""
    name = clean_name(name)
    existing = db.query(Folder).filter(Folder.owner_id == user.id, Folder.name_key == key(name)).first()
    return existing or create(db, user, name)


def rename(db: Session, user: User, folder: Folder, name: str) -> Folder:
    name = clean_name(name)
    clash = db.query(Folder).filter(Folder.owner_id == user.id, Folder.name_key == key(name),
                                    Folder.id != folder.id).first()
    if clash:
        raise FolderError("You already have a folder with that name", 409)
    folder.name, folder.name_key = name, key(name)
    return folder


def delete(db: Session, folder: Folder) -> int:
    """Remove the folder only. Its meetings are kept (they become unfiled); returns how many."""
    n = db.query(Meeting).filter(Meeting.folder_id == folder.id).update({Meeting.folder_id: None},
                                                                        synchronize_session=False)
    db.delete(folder)
    return n


def when(m: Meeting) -> datetime:
    """A meeting's place in time: the day it was scheduled for, else when it was uploaded."""
    return m.scheduled_time or m.started_at or m.created_at or datetime.min


def ordered(db: Session, folder_id: str) -> List[Meeting]:
    """The folder's meetings, oldest first (Meeting 1 is the first)."""
    rows = db.query(Meeting).filter(Meeting.folder_id == folder_id).all()
    return sorted(rows, key=lambda m: (when(m), m.created_at or datetime.min, m.id))


def number_of(db: Session, meeting: Meeting) -> Optional[int]:
    if not meeting.folder_id:
        return None
    for i, m in enumerate(ordered(db, meeting.folder_id), 1):
        if m.id == meeting.id:
            return i
    return None


def assign(db: Session, user: User, meeting: Meeting, folder_id: Optional[str]) -> Optional[Folder]:
    """Put the meeting in the user's folder (or take it out with None). Only the owner's own meetings."""
    if not folder_id:
        meeting.folder_id = None
        return None
    folder = get_owned(db, user, folder_id)
    if (meeting.owner_email or "").lower() != user.email.lower():
        raise FolderError("Only the meeting's owner can put it in a folder", 403)
    meeting.folder_id = folder.id
    return folder


def _clip(text: Optional[str], limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def latest_summaries(db: Session, meeting_ids: List[str]) -> Dict[str, Summary]:
    out: Dict[str, Summary] = {}
    if not meeting_ids:
        return out
    for s in db.query(Summary).filter(Summary.meeting_id.in_(meeting_ids)).order_by(Summary.created_at):
        out[s.meeting_id] = s                        # the newest wins
    return out


def previous_meetings(db: Session, meeting: Meeting) -> List[Meeting]:
    """The meetings in the same folder that come before this one, oldest first."""
    if not meeting.folder_id:
        return []
    out = []
    for m in ordered(db, meeting.folder_id):
        if m.id == meeting.id:
            break
        if m.status != "cancelled":
            out.append(m)
    return out


def _decision_texts(summary: Summary) -> List[str]:
    out = []
    for d in summary.decisions or []:
        text = d.get("decision") if isinstance(d, dict) else d
        if text:
            out.append(_clip(str(text), 120))
    return out[:4]


def memory_text(db: Session, meeting: Meeting) -> Optional[str]:
    """
    What the earlier meetings in this meeting's folder said, for the summary step: their summaries and
    decisions (the latest MEMORY_MEETINGS of them) and the tasks still open. None if nothing came before.
    """
    previous = previous_meetings(db, meeting)
    if not previous:
        return None
    folder = db.get(Folder, meeting.folder_id)
    summaries = latest_summaries(db, [m.id for m in previous])
    everything = ordered(db, meeting.folder_id)
    numbers = {m.id: i for i, m in enumerate(everything, 1)}

    header = f'Earlier meetings in the folder "{folder.name if folder else "this folder"}", oldest first:'
    used = [m for m in previous if m.id in summaries][-MEMORY_MEETINGS:]
    if not used:
        return None
    meeting_lines = []
    for m in used:
        s = summaries[m.id]
        line = f"Meeting {numbers[m.id]} - {m.title} ({when(m):%d %b %Y}): {_clip(s.executive_summary, SUMMARY_CHARS)}"
        decisions = _decision_texts(s)
        if decisions:
            line += " Decisions: " + "; ".join(decisions) + "."
        meeting_lines.append(line)

    open_tasks = (db.query(ActionItemDB)
                  .filter(ActionItemDB.meeting_id.in_([m.id for m in previous]), ActionItemDB.status != "completed")
                  .all())
    open_tasks.sort(key=lambda a: (a.due_date is None, a.due_date or datetime.max))
    task_lines = []
    for a in open_tasks[:MEMORY_OPEN_TASKS]:
        who = a.assignee if a.assignee and a.assignee != "TBD" else "unassigned"
        due = f", due {a.due_date:%d %b %Y}" if a.due_date else ""
        task_lines.append(f"- {who}: {_clip(a.task, 140)}{due} (from Meeting {numbers.get(a.meeting_id, '?')})")
    return _fit(header, meeting_lines, task_lines, MEMORY_MAX_CHARS)


def _fit(header: str, meetings: List[str], tasks: List[str], limit: int) -> str:
    """
    Keep the memory within `limit` characters by dropping the least important parts first: the tasks due
    last, then the oldest meetings. The newest earlier meeting is always kept (clipped as a last resort).
    """
    meetings, tasks = list(meetings), list(tasks)

    def render() -> str:
        lines = [header, *meetings]
        if tasks:
            lines += ["Tasks from earlier meetings that are still open:", *tasks]
        return "\n".join(lines)

    while len(render()) > limit and tasks:
        tasks.pop()
    while len(render()) > limit and len(meetings) > 1:
        meetings.pop(0)
    text = render()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def timeline(db: Session, folder: Folder) -> List[Dict[str, Any]]:
    """The folder's meetings in order, numbered, each with a short summary."""
    meetings = ordered(db, folder.id)
    summaries = latest_summaries(db, [m.id for m in meetings])
    counts: Dict[str, Dict[str, int]] = {m.id: {"open": 0, "done": 0} for m in meetings}
    for a in db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(list(counts))):
        counts[a.meeting_id]["done" if a.status == "completed" else "open"] += 1
    out = []
    for i, m in enumerate(meetings, 1):
        s = summaries.get(m.id)
        out.append({
            "number": i, "id": m.id, "title": m.title, "status": m.status,
            "when": when(m).isoformat() if when(m) != datetime.min else None,
            "has_summary": s is not None,
            "summary": s.executive_summary if s else None,
            "follow_up": s.follow_up if s else None,
            "open_tasks": counts[m.id]["open"], "done_tasks": counts[m.id]["done"],
        })
    return out
