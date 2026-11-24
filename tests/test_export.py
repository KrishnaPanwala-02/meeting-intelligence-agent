"""Calendar (.ics) export and the summary e-mail."""

import uuid
from datetime import datetime, timedelta

import pytest

from src.models.database import ActionItemDB, Meeting, Summary, User
from src.services import ical, mailer, summary_email
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401


# ------------------------------------------------------------------ iCalendar

def unfold(text):
    return text.replace("\r\n ", "").split("\r\n")


def test_escape_text_values():
    assert ical.escape("a, b; c\\d") == "a\\, b\; c\\\\d"
    assert ical.escape("line1\nline2\r\nline3") == "line1\\nline2\\nline3"
    assert ical.escape("bell\x07 and\x00nul") == "bell  and nul"
    assert ical.escape(None) == ""


def test_long_lines_fold_at_75_octets_without_breaking_characters():
    for text in ("x" * 300, "é" * 200, "日本語" * 60, "mix ✓ " * 50):
        line = "SUMMARY:" + text
        folded = ical.fold(line)
        parts = folded.split("\r\n")
        assert all(len(p.encode("utf-8")) <= 75 for p in parts)
        assert all(p.startswith(" ") for p in parts[1:])
        assert folded.replace("\r\n ", "") == line                       # unfolds exactly
    assert ical.fold("short") == "short"


def meeting(**kw):
    base = dict(id="m1", title="Weekly, sync; \"quarterly\"", owner_email="k@example.com", status="scheduled",
                participants=["r@example.com", "Anna"], scheduled_time=datetime(2026, 11, 13, 10, 0), duration_minutes=45,
                created_at=datetime(2026, 11, 1, 8, 0))
    base.update(kw)
    return Meeting(**base)


def test_a_scheduled_meeting_becomes_an_event_at_floating_local_time():
    ev = ical.meeting_event(meeting(), now=datetime(2026, 11, 2, 12, 30, 5), link="http://x/meetings/m1")
    assert ev[0] == "BEGIN:VEVENT" and ev[-1] == "END:VEVENT"
    assert "UID:meeting-m1@meeting-intelligence.local" in ev
    assert "DTSTAMP:20261102T123005Z" in ev
    assert "DTSTART:20261113T100000" in ev and "DTEND:20261113T104500" in ev      # no Z: floating
    assert "SUMMARY:Weekly\\, sync\; \"quarterly\"" in ev and "STATUS:CONFIRMED" in ev
    desc = next(l for l in ev if l.startswith("DESCRIPTION:"))
    assert "Organiser: k@example.com" in desc and "Participants: r@example.com\\, Anna" in desc and "http://x/meetings/m1" in desc


def test_a_recorded_meeting_is_placed_at_its_recording_time_in_utc():
    ev = ical.meeting_event(meeting(scheduled_time=None, status="uploaded", duration_minutes=2, started_at=None))
    assert "DTSTART:20261101T080000Z" in ev and "DTEND:20261101T080200Z" in ev


def test_a_cancelled_meeting_says_so():
    assert "STATUS:CANCELLED" in ical.meeting_event(meeting(status="cancelled"))


def task(**kw):
    base = dict(id="a1", task="Prepare the report, v2", assignee="Rahul Patel", due_date=datetime(2026, 11, 16),
                priority="high", status="pending")
    base.update(kw)
    return ActionItemDB(**base)


def test_a_task_deadline_is_an_all_day_event():
    ev = ical.task_event(task(), "ZOMATO", now=datetime(2026, 11, 2))
    assert "DTSTART;VALUE=DATE:20261116" in ev and "DTEND;VALUE=DATE:20261117" in ev
    assert "SUMMARY:Task due: Prepare the report\\, v2" in ev and "PRIORITY:1" in ev and "TRANSP:TRANSPARENT" in ev
    assert "UID:task-a1@meeting-intelligence.local" in ev
    assert "Assigned to: Rahul Patel" in next(l for l in ev if l.startswith("DESCRIPTION:"))
    month_end = ical.task_event(task(due_date=datetime(2026, 12, 31)), "Z")
    assert "DTEND;VALUE=DATE:20270101" in month_end                      # rolls into the next year
    assert "Assigned to" not in next(l for l in ical.task_event(task(assignee="TBD"), "Z") if l.startswith("DESCRIPTION:"))


def test_the_calendar_wrapper_is_valid():
    out = ical.build_calendar([ical.meeting_event(meeting())], "My, cal")
    assert out.endswith("END:VCALENDAR\r\n") and "\n" not in out.replace("\r\n", "")
    lines = unfold(out)[:-1]
    assert lines[:2] == ["BEGIN:VCALENDAR", "VERSION:2.0"] and "X-WR-CALNAME:My\\, cal" in lines
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 1
    assert ical.build_calendar([]).count("VEVENT") == 0


# --------------------------------------------------------------------- the API

def add_meeting(db, title, owner, *, participants=(), scheduled=None, status="scheduled", tasks=()):
    mid = str(uuid.uuid4())
    db.add(Meeting(id=mid, title=title, owner_email=owner, status=status, participants=list(participants),
                   scheduled_time=scheduled, duration_minutes=60, created_at=datetime(2026, 11, 1)))
    for text, who, st, due in tasks:
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task=text, assignee=who, status=st, priority="medium", due_date=due))
    db.commit()
    return mid


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"), "x": signup(client, "x@example.com")}
    client.patch("/auth/me", json={"name": "Krishna Panwala"}, headers=h["k"])
    client.patch("/auth/me", json={"name": "Rahul Patel"}, headers=h["r"])
    d = datetime(2026, 11, 16)
    with Session() as db:
        ids = {
            "zomato": add_meeting(db, "ZOMATO", "k@example.com", participants=["r@example.com"], scheduled=datetime(2026, 11, 20, 9, 0),
                                  tasks=[("Train the model", "Krishna Panwala", "pending", d), ("Write the report", "Rahul Patel", "pending", d),
                                         ("Done thing", "Krishna Panwala", "completed", d), ("No deadline", "Krishna Panwala", "pending", None)]),
            "cancelled": add_meeting(db, "Cancelled one", "k@example.com", scheduled=datetime(2026, 11, 21, 9, 0), status="cancelled"),
            "recorded": add_meeting(db, "Recorded only", "k@example.com", status="uploaded"),
            "secret": add_meeting(db, "Secret plans", "x@example.com", scheduled=datetime(2026, 11, 22, 9, 0),
                                  tasks=[("Guard the vault", "Rahul Patel", "pending", d)]),
        }
    return type("W", (), dict(h=h, ids=ids, client=client, Session=Session))


def fetch(w, who, path="/api/v1/export/calendar.ics", **params):
    r = w.client.get(path, params=params, headers=w.h[who])
    assert r.status_code == 200, r.text
    return r


def test_calendar_export_is_a_downloadable_ics_with_meetings_and_my_tasks(world):
    r = fetch(world, "k")
    assert r.headers["content-type"].startswith("text/calendar") and "meeting-intelligence.ics" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    lines = unfold(r.text)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR"
    summaries = [l for l in lines if l.startswith("SUMMARY:")]
    assert summaries == ["SUMMARY:ZOMATO", "SUMMARY:Task due: Train the model"]       # not cancelled, recorded, done, undated or others'


def test_everyone_sees_only_their_own_tasks_unless_they_ask_for_all(world):
    assert [l for l in unfold(fetch(world, "r").text) if l.startswith("SUMMARY:")] == ["SUMMARY:ZOMATO", "SUMMARY:Task due: Write the report"]
    every = [l for l in unfold(fetch(world, "k", mine="false").text) if l.startswith("SUMMARY:")]
    assert "SUMMARY:Task due: Write the report" in every and "SUMMARY:Task due: Train the model" in every


def test_filters_choose_meetings_or_tasks(world):
    assert [l for l in unfold(fetch(world, "k", tasks="false").text) if l.startswith("SUMMARY:")] == ["SUMMARY:ZOMATO"]
    assert [l for l in unfold(fetch(world, "k", meetings="false").text) if l.startswith("SUMMARY:")] == ["SUMMARY:Task due: Train the model"]
    empty = fetch(world, "k", meetings="false", tasks="false").text
    assert "VEVENT" not in empty and empty.startswith("BEGIN:VCALENDAR")


def test_the_export_never_includes_other_peoples_meetings(world):
    for who in ("k", "r"):
        for params in ({}, {"mine": "false"}):
            text = fetch(world, who, **params).text
            assert "Secret plans" not in text and "Guard the vault" not in text
    assert "Secret plans" in fetch(world, "x").text and "ZOMATO" not in fetch(world, "x").text
    assert world.client.get("/api/v1/export/calendar.ics").status_code == 401


def test_single_meeting_export(world):
    r = fetch(world, "k", f"/api/v1/meetings/{world.ids['zomato']}/calendar.ics")
    assert 'filename="ZOMATO.ics"' in r.headers["content-disposition"]
    summaries = [l for l in unfold(r.text) if l.startswith("SUMMARY:")]
    assert summaries[0] == "SUMMARY:ZOMATO" and set(summaries[1:]) == {"SUMMARY:Task due: Train the model", "SUMMARY:Task due: Write the report"}
    rec = fetch(world, "k", f"/api/v1/meetings/{world.ids['recorded']}/calendar.ics")
    assert "DTSTART:20261101T000000Z" in unfold(rec.text)
    assert world.client.get(f"/api/v1/meetings/{world.ids['secret']}/calendar.ics", headers=world.h["k"]).status_code == 404
    assert world.client.get(f"/api/v1/meetings/{world.ids['zomato']}/calendar.ics", headers=world.h["x"]).status_code == 404
    assert world.client.get(f"/api/v1/meetings/{world.ids['zomato']}/calendar.ics", headers=world.h["r"]).status_code == 200   # participants can


# ------------------------------------------------------------ the summary e-mail

def make_summary(db, meeting_id):
    db.add(Summary(id=str(uuid.uuid4()), meeting_id=meeting_id, created_at=datetime.utcnow(),
                   executive_summary="Krishna trains the model.\nRahul writes the report.",
                   key_points=[{"topic": "Model", "description": "Training ends Friday"}],
                   decisions=[{"decision": "Ship Friday", "owner": "Rahul Patel", "context": ""}],
                   next_steps=["Rahul sends the invite"], key_quotes=[]))
    db.commit()


def test_the_email_puts_the_recipients_own_tasks_first(world):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
        m = db.get(Meeting, world.ids["zomato"])
        s = db.query(Summary).one()
        acts = db.query(ActionItemDB).filter_by(meeting_id=m.id).order_by(ActionItemDB.id).all()
        rahul = db.query(User).filter(User.email == "r@example.com").one()
        subject, body = summary_email.compose(m, s, acts, "r@example.com", rahul, "Krishna Panwala", note="Please read.")
    assert subject == "Summary: ZOMATO"
    assert body.startswith("Hi Rahul,") and "Krishna Panwala shared the summary of “ZOMATO”" in body and "Please read." in body
    assert "Krishna trains the model. Rahul writes the report." in body          # newlines flattened
    assert body.index("YOUR TASKS") < body.index("Write the report") < body.index("ALL OTHER TASKS")
    mine, others = body.split("ALL OTHER TASKS")
    assert "Write the report" in mine and "Train the model" not in mine and "Train the model" in others
    assert "KEY POINTS" in body and "Ship Friday (Rahul Patel)" in body and "NEXT STEPS" in body
    assert "http://localhost:5173/meetings/" in body


def test_an_email_to_someone_without_an_account_lists_all_tasks(world):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
        m, s = db.get(Meeting, world.ids["zomato"]), db.query(Summary).one()
        acts = db.query(ActionItemDB).filter_by(meeting_id=m.id).all()
        _, body = summary_email.compose(m, s, acts, "guest@example.com", None, "Krishna Panwala")
    assert body.startswith("Hi,") and "YOUR TASKS" not in body and "\nTASKS\n" in body


def test_mail_headers_cannot_be_injected_through_a_title():
    m = Meeting(id="m", title="Hello\r\nBcc: evil@example.com", scheduled_time=datetime(2026, 11, 1))
    s = Summary(executive_summary="x", key_points=[], decisions=[], next_steps=[])
    subject, body = summary_email.compose(m, s, [], "a@b.co", None, "Sender\nBcc: x@y.z")
    assert "\n" not in subject and "\r" not in subject
    assert "shared the summary of “Hello Bcc: evil@example.com”" in body and "Sender Bcc: x@y.z" in body


@pytest.fixture
def outbox(monkeypatch):
    box = type("Box", (), {"sent": [], "fail": set()})()
    monkeypatch.setattr(mailer, "email_enabled", lambda: True)

    def fake(to, subject, body):
        if to in box.fail:
            return False
        box.sent.append((to, subject, body))
        return True

    monkeypatch.setattr(mailer, "send_email", fake)
    return box


def send(w, who, meeting="zomato", **body):
    return w.client.post(f"/api/v1/meetings/{w.ids[meeting]}/send-summary", json=body, headers=w.h[who])


def test_features_report_whether_email_works(world, monkeypatch):
    assert world.client.get("/api/v1/features", headers=world.h["k"]).json() == {"email_enabled": False}
    monkeypatch.setattr(mailer, "email_enabled", lambda: True)
    assert world.client.get("/api/v1/features", headers=world.h["k"]).json() == {"email_enabled": True}
    assert world.client.get("/api/v1/features").status_code == 401


def test_sending_goes_to_participants_with_their_own_task_lists(world, outbox):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
    r = send(world, "k", note="Have a look")
    assert r.status_code == 200 and r.json() == {"sent": ["r@example.com"], "failed": []}
    to, subject, body = outbox.sent[0]
    assert to == "r@example.com" and subject == "Summary: ZOMATO" and body.startswith("Hi Rahul,") and "Have a look" in body
    assert "YOUR TASKS" in body and "Krishna Panwala shared" in body


def test_chosen_recipients_must_already_have_access(world, outbox):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
        db.get(Meeting, world.ids["zomato"]).participants = ["r@example.com", "Anna"]       # a name is not an address
        db.commit()
    assert send(world, "k", recipients=["stranger@example.com"]).status_code == 422
    assert send(world, "k", recipients=["not-an-email"]).status_code == 422
    assert send(world, "k", recipients=["R@Example.com", "r@example.com"]).json()["sent"] == ["r@example.com"]   # case, duplicates
    assert send(world, "k", recipients=["k@example.com"]).json()["sent"] == ["k@example.com"]                     # the owner may copy themselves
    assert [s[0] for s in outbox.sent] == ["r@example.com", "k@example.com"]
    assert send(world, "k", recipients=[]).status_code == 422


def test_partial_and_total_failures(world, outbox):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
        db.get(Meeting, world.ids["zomato"]).participants = ["r@example.com", "bad@example.com"]
        db.commit()
    outbox.fail = {"bad@example.com"}
    assert send(world, "k").json() == {"sent": ["r@example.com"], "failed": ["bad@example.com"]}
    outbox.fail = {"bad@example.com", "r@example.com"}
    r = send(world, "k")
    assert r.status_code == 502 and "SMTP" in r.json()["detail"]


def test_only_the_owner_can_send_and_it_needs_a_summary_and_smtp(world, outbox, monkeypatch):
    assert send(world, "k").status_code == 409                                    # no summary yet
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
    assert send(world, "r").status_code == 403                                    # a participant
    assert send(world, "x").status_code == 404                                    # a stranger
    assert world.client.post(f"/api/v1/meetings/{world.ids['zomato']}/send-summary", json={}).status_code == 401
    assert send(world, "k", note="x" * 501).status_code == 422
    assert send(world, "k", recipients=["a@b.co"] * 21).status_code == 422
    monkeypatch.setattr(mailer, "email_enabled", lambda: False)
    r = send(world, "k")
    assert r.status_code == 503 and "not set up" in r.json()["detail"]
    assert outbox.sent == []


def test_a_meeting_with_nobody_to_send_to_is_explained(world, outbox):
    with world.Session() as db:
        make_summary(db, world.ids["zomato"])
        db.get(Meeting, world.ids["zomato"]).participants = []
        db.commit()
    r = send(world, "k")
    assert r.status_code == 422 and "Add participants" in r.json()["detail"]
