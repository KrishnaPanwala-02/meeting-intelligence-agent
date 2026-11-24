"""
Who a task belongs to.

Assignees are free text ("Krishna Panwala", "Rahul", "Speaker 2", "TBD"), so
ownership is decided by comparing the assignee with the signed-in user's name
and e-mail. Only the assignee may change a task's status; the meeting owner
may do so only for tasks nobody has claimed yet, and an admin for any task.
"""

import re
from typing import Dict, Iterable, Optional, Set

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, User
from . import auth

UNCLAIMED = {"", "tbd", "tba", "unassigned", "unknown", "n a", "none", "nobody", "everyone", "team"}
_SPEAKER = re.compile(r"^speaker \d+$")


def norm(text: Optional[str]) -> str:
    """Case-folded, punctuation-free, single-spaced."""
    return " ".join(re.sub(r"[\W_]+", " ", (text or "").casefold()).split())


def is_unclaimed(assignee: Optional[str]) -> bool:
    a = norm(assignee)
    return a in UNCLAIMED or bool(_SPEAKER.match(a))


def first_name(user_name: Optional[str]) -> str:
    parts = norm(user_name).split()
    return parts[0] if parts else ""


def peer_first_names(db: Session, meeting: Meeting, user: User) -> Set[str]:
    """First names of the other people on this meeting (to spot ambiguity)."""
    emails = {e for e in auth._participants(meeting)}
    if meeting.owner_email:
        emails.add(meeting.owner_email.lower())
    emails.discard(user.email.lower())
    if not emails:
        return set()
    rows = db.query(User.name).filter(User.email.in_(list(emails))).all()
    return {first_name(r[0]) for r in rows if r[0]}


def assignee_matches(user: User, assignee: Optional[str], peer_firsts: Iterable[str] = ()) -> bool:
    a = norm(assignee)
    if not a or is_unclaimed(a):
        return False
    if (assignee or "").strip().casefold() == (user.email or "").casefold():
        return True
    name = norm(user.name)
    if not name:
        return False
    if a == name:
        return True
    # "Krishna" alone: fine unless someone else on the meeting shares the first name.
    first = name.split()[0]
    return a == first and first not in set(peer_firsts)


def can_update_status(user: User, meeting: Meeting, assignee: Optional[str],
                      peer_firsts: Iterable[str] = ()) -> bool:
    if not auth.can_read(user, meeting):
        return False
    if auth.is_admin(user):
        return True
    if assignee_matches(user, assignee, peer_firsts):
        return True
    return is_unclaimed(assignee) and auth.can_write(user, meeting)


class Context:
    """Per-request cache so list endpoints do one peer lookup per meeting."""

    def __init__(self, db: Session, user: User):
        self.db, self.user = db, user
        self._peers: Dict[str, Set[str]] = {}
        self._threads: Dict[str, Dict] = {}

    def peers(self, meeting: Meeting) -> Set[str]:
        if meeting.id not in self._peers:
            self._peers[meeting.id] = peer_first_names(self.db, meeting, self.user)
        return self._peers[meeting.id]

    def preload(self, items: Iterable[ActionItemDB]) -> None:
        """Fetch comment counts for many tasks at once (otherwise `view` asks one by one)."""
        from . import task_thread
        self._threads.update(task_thread.counts(self.db, [a.id for a in items]))

    def view(self, meeting: Meeting, item: ActionItemDB) -> Dict[str, object]:
        peers = self.peers(meeting)
        if item.id not in self._threads:
            from . import task_thread
            self._threads.update(task_thread.counts(self.db, [item.id]))
        t = self._threads[item.id]
        return {
            "mine": assignee_matches(self.user, item.assignee, peers),
            "can_update_status": can_update_status(self.user, meeting, item.assignee, peers),
            "comment_count": t["comments"],
            "pending_extension": t["pending"],
        }
