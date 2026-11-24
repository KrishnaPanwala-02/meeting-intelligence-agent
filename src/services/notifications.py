"""
In-app notifications (and, if a person opts in and SMTP works, an e-mail copy).

A notification never breaks the action that caused it: failures are logged and
swallowed. Reminders use a dedupe key, so scanning again never repeats one.
"""

import os
import uuid
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, Notification, User
from . import assignments, auth, mailer

MAX_LIST = 100
EMAIL_WINDOW = timedelta(hours=1)


def _link(path: str) -> str:
    return path


def readers(db: Session, meeting: Meeting) -> List[User]:
    """Registered people who belong to the meeting: its owner and its invited participants."""
    emails = {str(p).strip().lower() for p in (meeting.participants or []) if "@" in str(p)}
    if meeting.owner_email:
        emails.add(meeting.owner_email.lower())
    if not emails:
        return []
    return db.query(User).filter(User.email.in_(list(emails)), User.is_active.is_(True)).all()


def notify(
    db: Session,
    user: User,
    kind: str,
    title: str,
    body: Optional[str] = None,
    link: Optional[str] = None,
    meeting_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> Optional[Notification]:
    """Add a notification (not committed). Returns None if the same dedupe key already exists."""
    if dedupe_key and db.query(Notification.id).filter(
        Notification.user_id == user.id, Notification.dedupe_key == dedupe_key
    ).first():
        return None
    row = Notification(id=str(uuid.uuid4()), user_id=user.id, kind=kind, title=" ".join(title.split())[:200],
                       body=body, link=link, meeting_id=meeting_id, dedupe_key=dedupe_key,
                       created_at=datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def _safely(db: Session, what: str, fn) -> int:
    """Run a notification step; commit its rows, or roll them back and carry on."""
    try:
        n = fn()
        db.commit()
        return n or 0
    except Exception:
        db.rollback()
        logger.exception("Could not create {} notifications", what)
        return 0


def _who(actor: Optional[User]) -> str:
    return (actor.name or actor.email) if actor else "Meeting Intelligence"


def notify_assigned(db: Session, meeting: Meeting, items: Iterable[ActionItemDB], actor: Optional[User]) -> int:
    """Tell people about tasks that now carry their name (not the person who made the change)."""
    items = [a for a in items if a.assignee and not assignments.is_unclaimed(a.assignee)]
    if not items:
        return 0

    def run() -> int:
        people = readers(db, meeting)
        count = 0
        for a in items:
            for u in people:
                if actor is not None and u.id == actor.id:
                    continue
                peers = assignments.peer_first_names(db, meeting, u)
                if not assignments.assignee_matches(u, a.assignee, peers):
                    continue
                due = f" Due {a.due_date.strftime('%a %d %b')}." if a.due_date else ""
                row = notify(db, u, "task_assigned", f"New task: {a.task}",
                             f"{_who(actor)} assigned you a task in “{meeting.title}”.{due}",
                             f"/meetings/{meeting.id}?tab=actions", meeting.id,
                             dedupe_key=f"assigned:{a.id}:{assignments.norm(a.assignee)}")
                count += int(row is not None)
        return count

    return _safely(db, "task", run)


def notify_shared(db: Session, meeting: Meeting, emails: Iterable[str], actor: Optional[User]) -> int:
    """Tell registered people that a meeting was shared with them."""
    wanted = {e.strip().lower() for e in emails if e}
    if not wanted:
        return 0

    def run() -> int:
        count = 0
        for u in db.query(User).filter(User.email.in_(list(wanted)), User.is_active.is_(True)).all():
            if actor is not None and u.id == actor.id:
                continue
            row = notify(db, u, "meeting_shared", f"Shared with you: {meeting.title}",
                         f"{_who(actor)} gave you access to this meeting.",
                         f"/meetings/{meeting.id}", meeting.id, dedupe_key=f"shared:{meeting.id}")
            count += int(row is not None)
        return count

    return _safely(db, "sharing", run)


def notify_people(db: Session, meeting: Meeting, users: Iterable[User], kind: str, title: str,
                  body: str, link: str, dedupe: Optional[str] = None) -> int:
    def run() -> int:
        count = 0
        for u in users:
            count += int(notify(db, u, kind, title, body, link, meeting.id,
                                dedupe_key=f"{dedupe}:{u.id}" if dedupe else None) is not None)
        return count

    return _safely(db, kind, run)


# ---------------------------------------------------------------------------
# Reading and marking
# ---------------------------------------------------------------------------

def unread_count(db: Session, user: User) -> int:
    return db.query(Notification).filter(Notification.user_id == user.id, Notification.read_at.is_(None)).count()


def listing(db: Session, user: User, unread_only: bool = False, limit: int = 30) -> List[Notification]:
    q = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    return q.order_by(Notification.created_at.desc(), Notification.id).limit(min(max(limit, 1), MAX_LIST)).all()


def mark_read(db: Session, user: User, ids: Optional[List[str]] = None) -> int:
    q = db.query(Notification).filter(Notification.user_id == user.id, Notification.read_at.is_(None))
    if ids is not None:
        q = q.filter(Notification.id.in_(ids))
    n = q.update({Notification.read_at: datetime.utcnow()}, synchronize_session=False)
    db.commit()
    return n


def view(n: Notification) -> dict:
    return {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link,
            "meeting_id": n.meeting_id, "created_at": n.created_at.isoformat(), "read": n.read_at is not None}


# ---------------------------------------------------------------------------
# E-mail copies (opt-in)
# ---------------------------------------------------------------------------

def deliver_emails(db: Session, now: Optional[datetime] = None) -> int:
    """E-mail recent notifications to people who asked for it. Returns how many were sent."""
    if not mailer.email_enabled():
        return 0
    now = now or datetime.utcnow()
    base = os.getenv("APP_URL", "http://localhost:5173").rstrip("/")
    rows = (db.query(Notification, User).join(User, User.id == Notification.user_id)
            .filter(Notification.emailed_at.is_(None), Notification.created_at >= now - EMAIL_WINDOW,
                    User.email_notifications.is_(True)).order_by(Notification.created_at).limit(200).all())
    sent = 0
    for n, u in rows:
        text = f"{n.body or ''}\n\n{base}{n.link}\n" if n.link else f"{n.body or ''}\n"
        ok = mailer.send_email(u.email, n.title, text.strip() + "\n")
        n.emailed_at = now                       # even on failure: do not retry forever
        sent += int(ok)
    db.commit()
    return sent
