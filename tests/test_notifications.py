"""Notifications, reminders, e-mail copies, and the profile settings that control them."""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from src.models.database import ActionItemDB, Meeting, Notification, Transcript, User
from src.models.schemas import ActionItem
from src.services import mailer, notifications, pipeline, reminders
from tests.test_celery_pipeline import FakeExtractor
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

NOW = datetime(2026, 11, 13, 12, 0)             # a Friday noon, treated as UTC
REAL_LAZY_SCAN = reminders.maybe_scan_for


@pytest.fixture(autouse=True)
def no_lazy_scan(monkeypatch):
    """Reading the inbox normally refreshes reminders; tests turn that off to see exactly what they cause."""
    monkeypatch.setattr(reminders, "maybe_scan_for", lambda db, user, now=None: 0)


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"),
         "x": signup(client, "x@example.com")}
    client.patch("/auth/me", json={"name": "Krishna Panwala"}, headers=h["k"])
    client.patch("/auth/me", json={"name": "Rahul Patel"}, headers=h["r"])
    client.patch("/auth/me", json={"name": "Someone Else"}, headers=h["x"])
    mid = str(uuid.uuid4())
    with Session() as db:
        db.add(Meeting(id=mid, title="ZOMATO", owner_email="k@example.com", status="scheduled",
                       participants=["r@example.com"], created_at=NOW))
        db.commit()
    return type("W", (), dict(h=h, mid=mid, client=client, Session=Session))


def inbox(w, who, **q):
    r = w.client.get("/api/v1/notifications", params=q, headers=w.h[who])
    assert r.status_code == 200, r.text
    return r.json()


def titles(w, who):
    return [n["title"] for n in inbox(w, who)["items"]]


def add_task(db, mid, task, who, due=None, status="pending"):
    a = ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task=task, assignee=who, status=status,
                     priority="medium", due_date=due)
    db.add(a)
    db.commit()
    return a


def user(db, email):
    return db.query(User).filter(User.email == email).one()


# ------------------------------------------------------------------- basics

def test_notify_creates_one_row_and_dedupes(world):
    with world.Session() as db:
        r = user(db, "r@example.com")
        assert notifications.notify(db, r, "k", " A  title ", "b", "/x", dedupe_key="same") is not None
        assert notifications.notify(db, r, "k", "again", dedupe_key="same") is None
        db.commit()
        assert db.query(Notification).count() == 1 and db.query(Notification).one().title == "A title"
        assert notifications.notify(db, user(db, "k@example.com"), "k", "t", dedupe_key="same") is not None   # per person


def test_readers_are_the_owner_and_invited_registered_people(world):
    with world.Session() as db:
        m = db.get(Meeting, world.mid)
        m.participants = ["r@example.com", "Anna", "ghost@nowhere.test"]
        db.commit()
        assert sorted(u.email for u in notifications.readers(db, m)) == ["k@example.com", "r@example.com"]


# ------------------------------------------------------------------- tasks

def test_a_new_task_notifies_the_assignee_but_not_the_person_who_made_it(world):
    r = world.client.post(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"],
                          json={"task": "Write the report", "assignee": "Rahul Patel"})
    assert r.status_code == 201
    n = inbox(world, "r")["items"][0]
    assert n["kind"] == "task_assigned" and n["title"] == "New task: Write the report" and n["read"] is False
    assert "Krishna Panwala assigned you a task in “ZOMATO”" in n["body"]
    assert n["link"] == f"/meetings/{world.mid}?tab=actions" and n["meeting_id"] == world.mid
    assert inbox(world, "k")["items"] == [] and inbox(world, "x")["items"] == []


def test_no_one_is_notified_for_unassigned_tasks_or_for_strangers(world):
    for who in ("TBD", "Speaker 2", ""):
        world.client.post(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"], json={"task": f"t {who}", "assignee": who})
    world.client.post(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"], json={"task": "for x", "assignee": "Someone Else"})
    assert [titles(world, w) for w in ("k", "r", "x")] == [[], [], []]       # x has no access to this meeting


def test_changing_the_assignee_notifies_the_new_one_only_when_it_changes(world):
    aid = world.client.post(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"], json={"task": "T", "assignee": "TBD"}).json()["id"]
    url = f"/api/v1/meetings/{world.mid}/actions/{aid}"
    world.client.put(url, headers=world.h["k"], json={"priority": "high"})
    assert titles(world, "r") == []
    world.client.put(url, headers=world.h["k"], json={"assignee": "Rahul Patel"})
    assert titles(world, "r") == ["New task: T"]
    world.client.put(url, headers=world.h["k"], json={"assignee": "rahul   patel"})          # same person, spelled differently
    assert titles(world, "r") == ["New task: T"]
    world.client.put(url, headers=world.h["r"], json={"status": "completed"})
    assert titles(world, "r") == ["New task: T"]


def test_extracted_tasks_notify_assignees_and_the_owner_hears_about_unclear_deadlines(world):
    class Extractor(FakeExtractor):
        async def extract(self, transcript, use_ai=True, reference_date=None):
            return [ActionItem(task="Finish the report", assignee="Rahul Patel", due_date=datetime(2026, 11, 20), due_date_phrase="by Friday"),
                    ActionItem(task="Book the room", assignee="Krishna Panwala", due_date=None, due_date_phrase="soon"),
                    ActionItem(task="Plan the demo", assignee="Rahul Patel", due_date=None, due_date_phrase=None)]

    with world.Session() as db:
        db.add(Transcript(id="t1", meeting_id=world.mid, language="en", full_text="x", segments=[{"id": 0, "start_time": 0, "end_time": 3, "text": "hi", "speaker": "A"}],
                          word_count=1, speaker_count=1, created_at=NOW))
        db.commit()
        m = db.get(Meeting, world.mid)
        out = asyncio.run(pipeline.extract_actions_step(db, m, extractor=Extractor()))
        rows = {a.task: a for a in db.query(ActionItemDB).all()}
        assert rows["Book the room"].due_phrase == "soon"                                 # unclear: the wording is kept
        assert rows["Finish the report"].due_phrase is None and rows["Plan the demo"].due_phrase is None
    assert sorted(titles(world, "r")) == ["New task: Finish the report", "New task: Plan the demo"]
    k = inbox(world, "k")["items"]
    assert sorted(n["title"] for n in k) == ["1 deadline needs a date", "New task: Book the room"]
    review = [n for n in k if n["kind"] == "deadline_review"]
    assert len(review) == 1 and review[0]["link"] == "/tasks?review=1" and review[0]["title"] == "1 deadline needs a date"


def test_naming_a_speaker_notifies_the_person_and_shares_the_meeting(world):
    with world.Session() as db:
        db.add(Transcript(id="t1", meeting_id=world.mid, language="en", full_text="x", created_at=NOW, word_count=1, speaker_count=1,
                          segments=[{"id": 0, "start_time": 0, "end_time": 3, "text": "hi", "speaker": "Speaker 2"}]))
        add_task(db, world.mid, "Write the report", "Speaker 2")
    r = world.client.put(f"/api/v1/meetings/{world.mid}/speakers", headers=world.h["k"],
                         json={"label": "Speaker 2", "name": "Someone Else", "email": "x@example.com"})
    assert r.status_code == 200
    assert sorted(titles(world, "x")) == ["New task: Write the report", "Shared with you: ZOMATO"]
    assert titles(world, "k") == []


# ------------------------------------------------------------------ sharing

def test_scheduling_and_editing_participants_notify_registered_people(world):
    start = (datetime.now() + timedelta(days=3)).replace(microsecond=0).isoformat()
    r = world.client.post("/api/v1/meetings/schedule", headers=world.h["k"],
                          json={"title": "Planning", "participants": ["r@example.com", "nobody@else.test"], "scheduled_time": start})
    assert r.status_code == 201
    n = inbox(world, "r")["items"][0]
    assert n["title"] == "Shared with you: Planning" and n["kind"] == "meeting_shared" and "Krishna Panwala" in n["body"]
    mid = r.json()["id"]
    world.client.put(f"/api/v1/meetings/{mid}", headers=world.h["k"], json={"participants": ["r@example.com", "x@example.com"]})
    assert titles(world, "x") == ["Shared with you: Planning"]
    assert titles(world, "r") == ["Shared with you: Planning"]                   # not told twice
    assert titles(world, "k") == []


# ------------------------------------------------------------------ the inbox

def test_listing_counting_reading_and_deleting(world):
    with world.Session() as db:
        r = user(db, "r@example.com")
        for i in range(5):
            notifications.notify(db, r, "t", f"n{i}", "b", "/x")
            db.flush()
        notifications.notify(db, user(db, "k@example.com"), "t", "kn")
        db.commit()
    d = inbox(world, "r")
    assert d["unread"] == 5 and [n["title"] for n in d["items"]][0] in {"n4", "n3", "n2", "n1", "n0"} and len(d["items"]) == 5
    assert len(inbox(world, "r", limit=2)["items"]) == 2
    first = d["items"][0]["id"]
    kid = inbox(world, "k")["items"][0]["id"]
    r = world.client.post("/api/v1/notifications/read", headers=world.h["r"], json={"ids": [first, kid, "nope"]})
    assert r.json() == {"marked": 1, "unread": 4}                                 # someone else's id is ignored
    assert inbox(world, "k")["unread"] == 1
    assert len(inbox(world, "r", unread_only="true")["items"]) == 4
    assert world.client.post("/api/v1/notifications/read", headers=world.h["r"], json={}).json() == {"marked": 4, "unread": 0}
    assert all(n["read"] for n in inbox(world, "r")["items"])
    assert world.client.delete(f"/api/v1/notifications/{kid}", headers=world.h["r"]).status_code == 404     # not theirs
    assert world.client.delete(f"/api/v1/notifications/{first}", headers=world.h["r"]).status_code == 204
    assert len(inbox(world, "r")["items"]) == 4 and world.client.delete(f"/api/v1/notifications/{first}", headers=world.h["r"]).status_code == 404


def test_the_inbox_needs_signing_in_and_sane_limits(world):
    assert world.client.get("/api/v1/notifications").status_code == 401
    assert world.client.post("/api/v1/notifications/read", json={}).status_code == 401
    for limit in (0, 101, -1):
        assert world.client.get("/api/v1/notifications", params={"limit": limit}, headers=world.h["k"]).status_code == 422
    assert world.client.post("/api/v1/notifications/read", headers=world.h["k"], json={"ids": ["a"] * 201}).status_code == 422


# ----------------------------------------------------------------- reminders

def due_world(world, **kw):
    with world.Session() as db:
        for name, who, days in (("today", "Rahul Patel", 0), ("tomorrow", "Rahul Patel", 1), ("later", "Rahul Patel", 2),
                                ("late", "Rahul Patel", -3), ("yesterday", "Rahul Patel", -1), ("krishna's", "Krishna Panwala", 0),
                                ("nobody's", "TBD", 0)):
            add_task(db, world.mid, name, who, NOW.replace(hour=0) + timedelta(days=days))
        add_task(db, world.mid, "done", "Rahul Patel", NOW.replace(hour=0), status="completed")
        add_task(db, world.mid, "no date", "Rahul Patel", None)


def test_reminders_for_due_soon_and_overdue_tasks_go_to_the_assignee(world):
    due_world(world)
    with world.Session() as db:
        assert reminders.scan(db, NOW) == 5
    r = sorted(n["title"] for n in inbox(world, "r")["items"] if n["kind"] != "task_assigned")
    assert r == ["Due today: today", "Due tomorrow: tomorrow", "Overdue: late", "Overdue: yesterday"]
    assert [n["title"] for n in inbox(world, "k")["items"]] == ["Due today: krishna's"]
    body = next(n["body"] for n in inbox(world, "r")["items"] if n["title"] == "Overdue: late")
    assert "3 days late" in body and "“ZOMATO”" in body
    assert next(n["body"] for n in inbox(world, "r")["items"] if n["title"] == "Overdue: yesterday").endswith("1 day late.")
    assert inbox(world, "x")["items"] == []


def test_scanning_again_does_not_repeat_reminders(world):
    due_world(world)
    with world.Session() as db:
        assert reminders.scan(db, NOW) == 5
        assert reminders.scan(db, NOW + timedelta(minutes=10)) == 0
        # The next day: "later" becomes due tomorrow, and two tasks that were due today are now overdue.
        # Reminders already sent for a due date ("tomorrow", "late", "yesterday") are not repeated.
        assert reminders.scan(db, NOW + timedelta(days=1)) == 3


def test_a_new_deadline_after_a_move_is_reminded_again(world):
    with world.Session() as db:
        a = add_task(db, world.mid, "T", "Rahul Patel", NOW.replace(hour=0))
        assert reminders.scan(db, NOW) == 1
        a.due_date = NOW.replace(hour=0) + timedelta(days=5)
        db.commit()
        assert reminders.scan(db, NOW) == 0
        assert reminders.scan(db, NOW + timedelta(days=5)) == 1


def test_a_persons_own_time_zone_decides_what_today_is(world):
    with world.Session() as db:
        user(db, "r@example.com").timezone = "Pacific/Auckland"       # already Saturday there
        add_task(db, world.mid, "T", "Rahul Patel", datetime(2026, 11, 13))
        db.commit()
        late_night_utc = datetime(2026, 11, 13, 12, 0)                 # Sat 01:00 in Auckland
        reminders.scan(db, late_night_utc)
    assert [n["title"] for n in inbox(world, "r")["items"] if n["kind"] != "task_assigned"] == ["Overdue: T"]


def test_meetings_starting_soon_remind_everyone_in_them(world):
    with world.Session() as db:
        m = db.get(Meeting, world.mid)
        m.scheduled_time, m.timezone = datetime(2026, 11, 13, 17, 45), "Asia/Kolkata"     # 12:15 UTC: 15 minutes after NOW
        db.commit()
        assert reminders.scan(db, NOW) == 2
        assert reminders.scan(db, NOW) == 0
    for who in ("k", "r"):
        n = inbox(world, who)["items"][0]
        assert n["title"] == "Starting soon: ZOMATO" and n["body"] == "Starts in 15 minutes." and n["link"] == f"/meetings/{world.mid}"
    assert inbox(world, "x")["items"] == []


@pytest.mark.parametrize("sched,tz,status,expected", [
    (datetime(2026, 11, 13, 12, 31), "UTC", "scheduled", 0),        # 31 minutes away
    (datetime(2026, 11, 13, 11, 59), "UTC", "scheduled", 0),        # already started
    (datetime(2026, 11, 13, 12, 30), "UTC", "scheduled", 2),        # exactly 30 minutes
    (datetime(2026, 11, 13, 12, 10), "UTC", "cancelled", 0),
    (datetime(2026, 11, 13, 12, 10), "UTC", "uploaded", 0),
    (datetime(2026, 11, 13, 12, 10), "America/New_York", "scheduled", 0),   # that is 17:10 UTC
    (None, None, "scheduled", 0),
])
def test_meeting_reminder_window(world, sched, tz, status, expected):
    with world.Session() as db:
        m = db.get(Meeting, world.mid)
        m.scheduled_time, m.timezone, m.status = sched, tz, status
        db.commit()
        assert reminders.scan(db, NOW) == expected


def test_scanning_for_one_person_only_creates_theirs(world):
    due_world(world)
    with world.Session() as db:
        assert reminders.scan(db, NOW, user=user(db, "k@example.com")) == 1
    assert titles(world, "r") == []


def test_opening_the_inbox_runs_a_throttled_scan(world, monkeypatch):
    monkeypatch.setattr(reminders, "maybe_scan_for", REAL_LAZY_SCAN)
    with world.Session() as db:
        add_task(db, world.mid, "T", "Rahul Patel", datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    reminders._last_scan.clear()
    assert [n["title"] for n in inbox(world, "r")["items"] if n["kind"] != "task_assigned"] == ["Due today: T"]
    with world.Session() as db:
        db.query(Notification).delete()
        db.commit()
    assert inbox(world, "r")["items"] == []                                      # throttled: no rescan within minutes
    with world.Session() as db:
        assert reminders.maybe_scan_for(db, user(db, "r@example.com"), now=10 ** 12) == 1


# -------------------------------------------------------------- e-mail copies

@pytest.fixture
def outbox(monkeypatch):
    box = type("Box", (), {"sent": [], "ok": True, "on": True})()
    monkeypatch.setattr(mailer, "email_enabled", lambda: box.on)
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, body: (box.sent.append((to, subject, body)) or box.ok))
    return box


def test_emails_go_only_to_people_who_opted_in_and_only_once(world, outbox):
    world.client.patch("/auth/me", json={"email_notifications": True}, headers=world.h["r"])
    with world.Session() as db:
        for u in ("r@example.com", "k@example.com"):
            notifications.notify(db, user(db, u), "t", "Hello", "Body text", "/tasks")
        db.commit()
        assert notifications.deliver_emails(db) == 1
        assert notifications.deliver_emails(db) == 0
    to, subject, body = outbox.sent[0]
    assert to == "r@example.com" and subject == "Hello" and "Body text" in body and "http://localhost:5173/tasks" in body
    assert len(outbox.sent) == 1


def test_email_copies_respect_the_server_setting_age_and_failures(world, outbox):
    world.client.patch("/auth/me", json={"email_notifications": True}, headers=world.h["r"])
    with world.Session() as db:
        r = user(db, "r@example.com")
        old = notifications.notify(db, r, "t", "Old")
        old.created_at = datetime.utcnow() - timedelta(hours=3)
        notifications.notify(db, r, "t", "New")
        db.commit()
        outbox.on = False
        assert notifications.deliver_emails(db) == 0 and outbox.sent == []
        outbox.on, outbox.ok = True, False
        assert notifications.deliver_emails(db) == 0                     # failed, but not retried forever
        outbox.ok = True
        assert notifications.deliver_emails(db) == 0 and [s[1] for s in outbox.sent] == ["New"]


# ---------------------------------------------------------------- the profile

def test_profile_holds_timezone_and_email_preference(world):
    c, h = world.client, world.h["k"]
    me = c.get("/auth/me", headers=h).json()
    assert me["timezone"] is None and me["email_notifications"] is False
    r = c.patch("/auth/me", json={"timezone": "Asia/Kolkata"}, headers=h).json()
    assert r["timezone"] == "Asia/Kolkata" and r["name"] == "Krishna Panwala"          # the name is untouched
    assert c.patch("/auth/me", json={"email_notifications": True}, headers=h).json()["email_notifications"] is True
    assert c.get("/auth/me", headers=h).json()["timezone"] == "Asia/Kolkata"
    assert c.patch("/auth/me", json={"timezone": ""}, headers=h).json()["timezone"] is None
    assert c.patch("/auth/me", json={"timezone": "Mars/Olympus"}, headers=h).status_code == 422
    assert c.patch("/auth/me", json={"timezone": "x" * 65}, headers=h).status_code == 422
    assert c.patch("/auth/me", json={}, headers=h).status_code == 422
    assert c.patch("/auth/me", json={"name": "  "}, headers=h).status_code == 422
    assert c.patch("/auth/me", json={"timezone": "UTC"}).status_code == 401


# ----------------------------------------------------------- the background cycle

def test_a_reminder_cycle_creates_reminders_and_sends_requested_emails(world, outbox, monkeypatch):
    from src.api import main
    import src.models.db as dbmod
    world.client.patch("/auth/me", json={"email_notifications": True}, headers=world.h["r"])
    with world.Session() as db:
        add_task(db, world.mid, "T", "Rahul Patel", datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    monkeypatch.setattr(dbmod, "SessionLocal", world.Session)
    assert main.run_reminder_cycle() >= 1
    assert any(s[0] == "r@example.com" and s[1] == "Due today: T" for s in outbox.sent)
    assert main.run_reminder_cycle() == 0                                          # nothing new the second time
