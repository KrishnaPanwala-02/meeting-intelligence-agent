"""
Numbers about meetings and tasks: who talks how much, who finishes what, and how
the weeks compare. Only meetings the user can read are counted.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, Transcript, User
from . import assignments, auth
from .ask import meeting_when
from .voices import DEFAULT_LABEL

MIN_DAYS, MAX_DAYS = 7, 365


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _transcript_seconds(t: Optional[Transcript]) -> float:
    return max((float(s.get("end_time") or 0) for s in (t.segments if t else None) or []), default=0.0)


class _People:
    """People by normalised name; a lone first name joins the one full name that starts with it."""

    def __init__(self, names: List[str]):
        self.spelling: Dict[str, str] = {}
        for n in names:
            key = assignments.norm(n)
            if key and key not in self.spelling:
                self.spelling[key] = " ".join(str(n).split())
        firsts: Dict[str, List[str]] = defaultdict(list)
        for key in self.spelling:
            if " " in key:
                firsts[key.split()[0]].append(key)
        self.first_to_full = {f: ks[0] for f, ks in firsts.items() if len(ks) == 1}

    def key(self, name: Optional[str]) -> Optional[str]:
        if assignments.is_unclaimed(name):
            return None
        key = assignments.norm(name)
        return self.first_to_full.get(key, key) if key and " " not in key else (key or None)

    def display(self, key: str) -> str:
        return self.spelling.get(key, key.title())


def compute(db: Session, user: User, days: int = 90, now: Optional[datetime] = None) -> Dict[str, Any]:
    days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    now = now or datetime.now()
    since = now - timedelta(days=days)

    meetings = [m for m in db.query(Meeting).all() if auth.can_read(user, m)]
    meetings = [m for m in meetings if (meeting_when(m) or datetime.min) >= since]
    ids = [m.id for m in meetings]
    by_id = {m.id: m for m in meetings}

    transcripts: Dict[str, Transcript] = {}
    for t in db.query(Transcript).filter(Transcript.meeting_id.in_(ids)).order_by(Transcript.created_at):
        transcripts[t.meeting_id] = t
    tasks = db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(ids)).all() if ids else []

    # -- who is who
    names: List[str] = []
    for t in transcripts.values():
        names += [s.get("speaker") for s in t.segments or [] if s.get("speaker") and not DEFAULT_LABEL.match(s["speaker"])]
    names += [a.assignee for a in tasks if a.assignee]
    people = _People([n for n in names if not assignments.is_unclaimed(n)])

    talk: Dict[str, float] = defaultdict(float)
    words: Dict[str, int] = defaultdict(int)
    attended: Dict[str, set] = defaultdict(set)
    unnamed_talk = 0.0
    recorded_seconds = 0.0
    for mid, t in transcripts.items():
        recorded_seconds += _transcript_seconds(t)
        for s in t.segments or []:
            label = s.get("speaker")
            span = max(0.0, float(s.get("end_time") or 0) - float(s.get("start_time") or 0))
            key = people.key(label) if label else None
            if key is None:
                unnamed_talk += span
                continue
            talk[key] += span
            words[key] += len((s.get("text") or "").split())
            attended[key].add(mid)

    today = now.date()
    stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "done": 0, "open": 0, "overdue": 0})
    totals = {"total": 0, "done": 0, "open": 0, "overdue": 0, "unassigned_open": 0}
    open_by_priority = {"high": 0, "medium": 0, "low": 0}
    for a in tasks:
        done = a.status == "completed"
        late = (not done) and a.due_date is not None and a.due_date.date() < today
        totals["total"] += 1
        totals["done" if done else "open"] += 1
        totals["overdue"] += int(late)
        if not done and a.priority in open_by_priority:
            open_by_priority[a.priority] += 1
        key = people.key(a.assignee)
        if key is None:
            totals["unassigned_open"] += int(not done)
            continue
        st = stats[key]
        st["total"] += 1
        st["done" if done else "open"] += 1
        st["overdue"] += int(late)

    total_talk = sum(talk.values()) + unnamed_talk
    rows = []
    for key in set(talk) | set(stats):
        st = stats.get(key, {"total": 0, "done": 0, "open": 0, "overdue": 0})
        rows.append({
            "name": people.display(key),
            "talk_seconds": round(talk.get(key, 0.0), 1),
            "talk_share": round(talk.get(key, 0.0) / total_talk, 4) if total_talk else 0.0,
            "words": words.get(key, 0),
            "meetings": len(attended.get(key, ())),
            "tasks_total": st["total"], "tasks_done": st["done"], "tasks_open": st["open"], "tasks_overdue": st["overdue"],
            "completion_rate": round(st["done"] / st["total"], 4) if st["total"] else None,
        })
    rows.sort(key=lambda r: (-r["talk_seconds"], -r["tasks_total"], r["name"].lower()))

    # -- weeks (Monday to Sunday), oldest first, including empty ones
    weeks: Dict[date, Dict[str, Any]] = {}
    cursor = _monday(since.date())
    while cursor <= today:
        weeks[cursor] = {"week_start": cursor.isoformat(), "meetings": 0, "tasks_created": 0,
                         "tasks_completed": 0, "talk_minutes": 0.0}
        cursor += timedelta(days=7)
    for m in meetings:
        w = weeks.get(_monday((meeting_when(m) or since).date()))
        if w is not None:
            w["meetings"] += 1
            w["talk_minutes"] += _transcript_seconds(transcripts.get(m.id)) / 60
    for a in tasks:
        created = (a.created_at or meeting_when(by_id[a.meeting_id]) or since).date()
        w = weeks.get(_monday(created))
        if w is not None:
            w["tasks_created"] += 1
        if a.status == "completed" and a.completed_at:
            w = weeks.get(_monday(a.completed_at.date()))
            if w is not None:
                w["tasks_completed"] += 1
    for w in weeks.values():
        w["talk_minutes"] = round(w["talk_minutes"], 1)

    return {
        "days": days,
        "overview": {
            "meetings": len(meetings),
            "meetings_with_transcript": len(transcripts),
            "recorded_seconds": round(recorded_seconds, 1),
            "tasks": totals["total"], "tasks_done": totals["done"], "tasks_open": totals["open"],
            "tasks_overdue": totals["overdue"], "tasks_unassigned_open": totals["unassigned_open"],
            "completion_rate": round(totals["done"] / totals["total"], 4) if totals["total"] else None,
            "unnamed_talk_seconds": round(unnamed_talk, 1),
        },
        "open_by_priority": open_by_priority,
        "people": rows,
        "weeks": list(weeks.values()),
    }
