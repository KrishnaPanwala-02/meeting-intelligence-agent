"""
Discussion on a task: comments, and requests to move its deadline.

Anyone who can read the meeting may comment. Only the person the task is assigned
to may ask for more time; only the meeting owner (or an admin) answers. An approved
request moves the deadline.
"""

import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, TaskComment, User
from . import assignments, auth, notifications

MAX_COMMENT = 1000
MAX_REASON = 500
MAX_PER_TASK = 200


class ThreadError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _text(value: Optional[str], limit: int, what: str, required: bool = True) -> Optional[str]:
    s = (value or "").strip()
    s = "\n".join(" ".join(line.split()) for line in s.splitlines()).strip()
    while "\n\n\n" in s:                      # keep paragraph breaks, but not long runs of blank lines
        s = s.replace("\n\n\n", "\n\n")
    if not s:
        if required:
            raise ThreadError(f"{what} cannot be empty", 422)
        return None
    if len(s) > limit:
        raise ThreadError(f"{what} must be at most {limit} characters", 422)
    if any(not (ch.isprintable() or ch == "\n") for ch in s):
        raise ThreadError(f"{what} contains invalid characters", 422)
    return s


def _users(db: Session, ids: List[str]) -> Dict[str, User]:
    return {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}


def _who(u: Optional[User]) -> Dict[str, Any]:
    return {"id": u.id, "name": u.name, "email": u.email} if u else {"id": None, "name": None, "email": None}


def view(c: TaskComment, users: Dict[str, User], me: User) -> Dict[str, Any]:
    return {
        "id": c.id, "kind": c.kind, "body": c.body, "author": _who(users.get(c.user_id)),
        "mine": c.user_id == me.id, "created_at": c.created_at.isoformat(),
        "requested_due": c.requested_due.date().isoformat() if c.requested_due else None,
        "status": c.status, "decided_by": _who(users.get(c.decided_by)) if c.decided_by else None,
        "decided_at": c.decided_at.isoformat() if c.decided_at else None, "decision_note": c.decision_note,
    }


def abilities(db: Session, user: User, meeting: Meeting, action: ActionItemDB) -> Dict[str, bool]:
    peers = assignments.peer_first_names(db, meeting, user)
    mine = assignments.assignee_matches(user, action.assignee, peers)
    pending = db.query(TaskComment.id).filter(TaskComment.action_id == action.id, TaskComment.status == "pending").first()
    return {
        "can_comment": auth.can_read(user, meeting),
        "can_request_extension": mine and action.status != "completed" and pending is None,
        "can_decide": auth.can_write(user, meeting),
    }


def thread(db: Session, user: User, meeting: Meeting, action: ActionItemDB) -> Dict[str, Any]:
    rows = (db.query(TaskComment).filter(TaskComment.action_id == action.id)
            .order_by(TaskComment.created_at, TaskComment.id).all())
    ids = {r.user_id for r in rows} | {r.decided_by for r in rows if r.decided_by}
    users = _users(db, list(ids))
    return {"comments": [view(r, users, user) for r in rows], **abilities(db, user, meeting, action)}


def counts(db: Session, action_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """{action id: {"comments": n, "pending": {...} | None}} for many tasks in two queries."""
    out = {i: {"comments": 0, "pending": None} for i in action_ids}
    if not action_ids:
        return out
    rows = db.query(TaskComment).filter(TaskComment.action_id.in_(action_ids)).all()
    users = _users(db, list({r.user_id for r in rows}))
    for r in rows:
        o = out[r.action_id]
        o["comments"] += 1
        if r.status == "pending":
            o["pending"] = {"id": r.id, "requested_due": r.requested_due.date().isoformat() if r.requested_due else None,
                            "by": _who(users.get(r.user_id))}
    return out


def _involved(db: Session, meeting: Meeting, action: ActionItemDB, author: User) -> List[User]:
    """People who should hear about activity on a task: its assignee, the owner and earlier commenters."""
    people = notifications.readers(db, meeting)
    commenters = {r[0] for r in db.query(TaskComment.user_id).filter(TaskComment.action_id == action.id)}
    out = []
    for u in people:
        if u.id == author.id:
            continue
        peers = assignments.peer_first_names(db, meeting, u)
        if (assignments.assignee_matches(u, action.assignee, peers) or auth.is_owner(u, meeting)
                or u.id in commenters):
            out.append(u)
    return out


def add_comment(db: Session, user: User, meeting: Meeting, action: ActionItemDB, body: str) -> TaskComment:
    text = _text(body, MAX_COMMENT, "The comment")
    if db.query(TaskComment).filter(TaskComment.action_id == action.id).count() >= MAX_PER_TASK:
        raise ThreadError("This task has too many comments", 409)
    c = TaskComment(id=str(uuid.uuid4()), action_id=action.id, user_id=user.id, kind="comment", body=text,
                    created_at=datetime.utcnow())
    db.add(c)
    db.commit()
    notifications.notify_people(
        db, meeting, _involved(db, meeting, action, user), "task_comment", f"New comment: {action.task}",
        f"{user.name or user.email}: {text[:140]}", f"/meetings/{meeting.id}?tab=actions&task={action.id}")
    return c


def delete_comment(db: Session, user: User, meeting: Meeting, comment: TaskComment) -> None:
    if comment.kind != "comment":
        raise ThreadError("Extension requests cannot be deleted", 409)
    if comment.user_id != user.id and not auth.can_write(user, meeting):
        raise ThreadError("You can only delete your own comments", 403)
    db.delete(comment)
    db.commit()


def request_extension(db: Session, user: User, meeting: Meeting, action: ActionItemDB,
                      requested_due: datetime, reason: Optional[str], today: Optional[date] = None) -> TaskComment:
    if not abilities(db, user, meeting, action)["can_request_extension"]:
        if action.status == "completed":
            raise ThreadError("This task is already done", 409)
        if db.query(TaskComment.id).filter(TaskComment.action_id == action.id, TaskComment.status == "pending").first():
            raise ThreadError("There is already a request waiting for an answer", 409)
        raise ThreadError("Only the person this task is assigned to can ask for more time", 403)
    today = today or date.today()
    new = requested_due.date()
    if new < today:
        raise ThreadError("Choose a date that is not in the past", 422)
    if action.due_date is not None and new <= action.due_date.date():
        raise ThreadError("Choose a date later than the current deadline", 422)
    c = TaskComment(id=str(uuid.uuid4()), action_id=action.id, user_id=user.id, kind="extension_request",
                    body=_text(reason, MAX_REASON, "The reason", required=False),
                    requested_due=datetime(new.year, new.month, new.day), status="pending",
                    created_at=datetime.utcnow())
    db.add(c)
    db.commit()
    owner = [u for u in notifications.readers(db, meeting) if auth.is_owner(u, meeting) and u.id != user.id]
    notifications.notify_people(
        db, meeting, owner, "extension_requested", f"More time asked: {action.task}",
        f"{user.name or user.email} asks to move the deadline to {new.strftime('%a %d %b %Y')}."
        + (f" “{c.body[:120]}”" if c.body else ""),
        f"/meetings/{meeting.id}?tab=actions&task={action.id}")
    return c


def decide(db: Session, user: User, meeting: Meeting, action: ActionItemDB, request_id: str,
           approve: bool, note: Optional[str]) -> TaskComment:
    if not auth.can_write(user, meeting):
        raise ThreadError("Only the meeting owner can answer a request", 403)
    req = (db.query(TaskComment).filter(TaskComment.id == request_id, TaskComment.action_id == action.id,
                                        TaskComment.kind == "extension_request").first())
    if req is None:
        raise ThreadError("Request not found", 404)
    if req.status != "pending":
        raise ThreadError("This request has already been answered", 409)
    req.status = "approved" if approve else "declined"
    req.decided_by, req.decided_at = user.id, datetime.utcnow()
    req.decision_note = _text(note, MAX_REASON, "The note", required=False)
    if approve:
        action.due_date = req.requested_due
        action.due_phrase = None
    db.commit()
    requester = db.query(User).filter(User.id == req.user_id).first()
    if requester is not None and requester.id != user.id:
        verdict = f"approved: the new deadline is {req.requested_due.strftime('%a %d %b %Y')}" if approve else "declined"
        notifications.notify_people(
            db, meeting, [requester], "extension_decided", f"Request {'approved' if approve else 'declined'}: {action.task}",
            f"{user.name or user.email} {verdict}." + (f" “{req.decision_note[:120]}”" if req.decision_note else ""),
            f"/meetings/{meeting.id}?tab=actions&task={action.id}")
    return req
