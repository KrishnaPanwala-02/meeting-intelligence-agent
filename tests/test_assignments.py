"""Task ownership: only the assignee ticks a task; the owner manages the rest."""

import uuid
from datetime import datetime

import pytest

from datetime import datetime
from src.models.database import ActionItemDB, Meeting, Summary, Transcript, User
from src.services import assignments
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401


def user(name, email="x@example.com"):
    return User(id="u", email=email, name=name, password_hash="x", is_active=True)


# ----------------------------------------------------------------- matching

@pytest.mark.parametrize("assignee,expected", [
    ("Krishna Panwala", True),
    ("krishna panwala", True),
    ("  KRISHNA   PANWALA. ", True),
    ("Krishna", True),                       # first name alone
    ("Krishna Patel", False),                # same first name, different person
    ("Rahul Patel", False),
    ("Speaker 1", False), ("TBD", False), ("", False), (None, False), ("Unassigned", False),
    ("x@example.com", True), ("X@Example.com", True),
])
def test_assignee_matches(assignee, expected):
    assert assignments.assignee_matches(user("Krishna Panwala"), assignee) is expected


def test_first_name_is_not_enough_when_a_peer_shares_it():
    u = user("Krishna Panwala")
    assert assignments.assignee_matches(u, "Krishna", {"krishna"}) is False
    assert assignments.assignee_matches(u, "Krishna Panwala", {"krishna"}) is True


def test_user_without_a_name_only_matches_by_email():
    u = user(None)
    assert assignments.assignee_matches(u, "Krishna") is False
    assert assignments.assignee_matches(u, "x@example.com") is True


def test_unclaimed_labels():
    for a in ("TBD", "tbd", "", None, "Speaker 3", "speaker 12", "unassigned"):
        assert assignments.is_unclaimed(a)
    assert not assignments.is_unclaimed("Rahul Patel")


# ----------------------------------------------------------------- the API

@pytest.fixture
def world(client, Session):
    h = {}
    h["k"] = signup(client, "k@example.com")
    h["r"] = signup(client, "r@example.com")
    h["x"] = signup(client, "stranger@example.com")
    h["admin"] = signup(client, "tester@example.com")
    client.patch("/auth/me", json={"name": "Krishna Panwala"}, headers=h["k"])
    client.patch("/auth/me", json={"name": "Rahul Patel"}, headers=h["r"])
    client.patch("/auth/me", json={"name": "Someone Else"}, headers=h["x"])
    mid = str(uuid.uuid4())
    ids = {}
    with Session() as db:
        db.add(Meeting(id=mid, title="ZOMATO", owner_email="k@example.com", status="completed",
                       participants=["r@example.com"]))
        for key, task, who in (("k", "Complete model training", "Krishna Panwala"),
                               ("r", "Prepare project report", "Rahul Patel"),
                               ("tbd", "Book a room", "TBD"),
                               ("sp", "Review notes", "Speaker 2"),
                               ("rf", "Send invites", "rahul")):
            ids[key] = str(uuid.uuid4())
            db.add(ActionItemDB(id=ids[key], meeting_id=mid, task=task, assignee=who,
                                status="pending", priority="medium"))
        db.commit()
    return type("W", (), dict(h=h, mid=mid, ids=ids, client=client, Session=Session))


def patch(w, who, key, status="completed"):
    return w.client.patch(f"/api/v1/meetings/{w.mid}/actions/{w.ids[key]}",
                          params={"status": status}, headers=w.h[who])


def put(w, who, key, **body):
    return w.client.put(f"/api/v1/meetings/{w.mid}/actions/{w.ids[key]}", json=body, headers=w.h[who])


def test_each_person_can_tick_only_their_own_tasks(world):
    assert patch(world, "k", "k").status_code == 200
    r = patch(world, "k", "r")
    assert r.status_code == 403 and "Rahul Patel" in r.json()["detail"]
    assert patch(world, "r", "r").status_code == 200
    r = patch(world, "r", "k")
    assert r.status_code == 403 and "Krishna Panwala" in r.json()["detail"]


def test_same_rules_over_put(world):
    assert put(world, "k", "k", status="completed").status_code == 200
    assert put(world, "k", "r", status="completed").status_code == 403
    assert put(world, "r", "r", status="in_progress").status_code == 200
    assert put(world, "r", "k", status="completed").status_code == 403


def test_a_refused_tick_changes_nothing(world):
    patch(world, "k", "r")
    with world.Session() as db:
        item = db.get(ActionItemDB, world.ids["r"])
        assert item.status == "pending" and item.completed_at is None


def test_first_name_assignee_belongs_to_that_person(world):
    assert patch(world, "r", "rf").status_code == 200       # "rahul"
    assert patch(world, "k", "rf").status_code == 403


def test_unclaimed_tasks_belong_to_the_owner_only(world):
    assert patch(world, "k", "tbd").status_code == 200
    assert patch(world, "k", "sp").status_code == 200       # still "Speaker 2"
    assert patch(world, "r", "tbd").status_code == 403
    assert patch(world, "r", "sp").status_code == 403


def test_admin_can_tick_anything_and_strangers_see_nothing(world):
    assert patch(world, "admin", "r").status_code == 200
    assert patch(world, "x", "r").status_code == 404


def test_reopening_follows_the_same_rule(world):
    patch(world, "r", "r")
    assert patch(world, "k", "r", "pending").status_code == 403
    assert patch(world, "r", "r", "pending").status_code == 200


def test_owner_still_manages_everyones_tasks_but_participants_cannot(world):
    assert put(world, "k", "r", task="Prepare the final report", priority="high").status_code == 200
    assert put(world, "k", "r", assignee="Krishna Panwala").status_code == 200   # reassign
    assert patch(world, "k", "r").status_code == 200                              # now hers
    # Rahul may not edit, even his own task's text, add or delete
    assert put(world, "r", "rf", task="x").status_code == 403
    assert put(world, "r", "rf", priority="low").status_code == 403
    assert put(world, "r", "rf", due_date="2026-12-01T00:00:00").status_code == 403
    assert put(world, "r", "rf", clear_due_date=True).status_code == 403
    assert put(world, "r", "rf").status_code == 403                               # empty body = edit
    assert world.client.delete(f"/api/v1/meetings/{world.mid}/actions/{world.ids['rf']}",
                               headers=world.h["r"]).status_code == 403
    assert world.client.post(f"/api/v1/meetings/{world.mid}/actions", json={"task": "New"},
                             headers=world.h["r"]).status_code == 403


def test_status_plus_edit_in_one_call_needs_both_rights(world):
    assert put(world, "r", "r", status="completed", priority="high").status_code == 403
    assert put(world, "k", "r", status="completed", priority="high").status_code == 403
    assert put(world, "k", "k", status="completed", priority="high").status_code == 200


def test_flags_are_per_viewer(world):
    def view(who):
        rows = world.client.get(f"/api/v1/meetings/{world.mid}/actions", headers=world.h[who]).json()["action_items"]
        return {r["task"]: (r["mine"], r["can_update_status"]) for r in rows}
    k, r = view("k"), view("r")
    assert k["Complete model training"] == (True, True)
    assert k["Prepare project report"] == (False, False)
    assert k["Book a room"] == (False, True)                 # unclaimed: owner may
    assert r["Prepare project report"] == (True, True)
    assert r["Send invites"] == (True, True)
    assert r["Book a room"] == (False, False)
    assert r["Complete model training"] == (False, False)

    allrows = world.client.get("/api/v1/actions", headers=world.h["r"]).json()["action_items"]
    assert {a["task"] for a in allrows} == {"Prepare project report", "Send invites"}      # only r's own
    assert all(a["mine"] and a["can_update_status"] for a in allrows)
    assert all("can_write" in a and a["can_write"] is False for a in allrows)


def test_put_response_carries_the_flags(world):
    out = put(world, "r", "r", status="completed").json()
    assert out["mine"] is True and out["can_update_status"] is True and out["status"] == "completed"


def test_shared_first_name_is_ambiguous(world):
    c = world.client
    h2 = signup(c, "krishna2@example.com")
    c.patch("/auth/me", json={"name": "Krishna Shah"}, headers=h2)
    with world.Session() as db:
        m = db.get(Meeting, world.mid)
        m.participants = ["r@example.com", "krishna2@example.com"]
        item = db.get(ActionItemDB, world.ids["tbd"])
        item.assignee = "Krishna"
        db.commit()
    assert patch(world, "k", "tbd").status_code == 403       # which Krishna? nobody may
    assert put(world, "k", "tbd", assignee="Krishna Panwala").status_code == 200
    assert patch(world, "k", "tbd").status_code == 200


# ---------------------------------------------------- inviting a named speaker

def seed_transcript(Session, mid):
    with Session() as db:
        db.add(Transcript(id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="x",
                          segments=[{"id": 0, "start_time": 0, "end_time": 2, "speaker": "Speaker 7", "text": "hi"}],
                          word_count=1, speaker_count=1, created_at=datetime.utcnow()))
        db.commit()


def test_naming_a_speaker_with_an_email_gives_that_person_access(world):
    c = world.client
    seed_transcript(world.Session, world.mid)
    url = f"/api/v1/meetings/{world.mid}/speakers"
    assert c.get(f"/api/v1/meetings/{world.mid}", headers=world.h["x"]).status_code == 404

    r = c.put(url, json={"label": "Speaker 7", "name": "Someone Else", "email": " Stranger@Example.COM "},
              headers=world.h["k"])
    assert r.status_code == 200 and r.json()["invited"] == "stranger@example.com"
    assert c.get(f"/api/v1/meetings/{world.mid}", headers=world.h["x"]).status_code == 200
    with world.Session() as db:
        assert db.get(Meeting, world.mid).participants == ["r@example.com", "stranger@example.com"]

    # inviting again, or the owner's own address, adds nothing new
    c.put(url, json={"label": "Someone Else", "name": "Someone Else", "email": "stranger@example.com"}, headers=world.h["k"])
    c.put(url, json={"label": "Someone Else", "name": "Someone E.", "email": "k@example.com"}, headers=world.h["k"])
    with world.Session() as db:
        assert db.get(Meeting, world.mid).participants == ["r@example.com", "stranger@example.com"]


def test_bad_email_is_rejected_before_anything_changes(world):
    seed_transcript(world.Session, world.mid)
    r = world.client.put(f"/api/v1/meetings/{world.mid}/speakers",
                         json={"label": "Speaker 7", "name": "Bob", "email": "not-an-email"},
                         headers=world.h["k"])
    assert r.status_code == 422
    labels = [s["label"] for s in world.client.get(
        f"/api/v1/meetings/{world.mid}/speakers", headers=world.h["k"]).json()["speakers"]]
    assert labels == ["Speaker 7"]


# ------------------------------------------------------- "mine" filter and owner info

def test_tasks_can_be_filtered_to_the_ones_assigned_to_me(world):
    def get(who, **q):
        return world.client.get("/api/v1/actions", params=q, headers=world.h[who]).json()
    mine = get("r", mine="true")
    assert {a["task"] for a in mine["action_items"]} == {"Prepare project report", "Send invites"}
    assert mine["total"] == 2 and all(a["mine"] for a in mine["action_items"])
    for q in ({}, {"mine": "false"}):                                # others' tasks can never be requested
        got = get("r", **q)
        assert got["total"] == 2 and all(a["mine"] for a in got["action_items"])
    assert get("k")["total"] == 1
    assert get("x")["total"] == 0


def test_mine_filter_pages_and_combines_with_status(world):
    one = world.client.get("/api/v1/actions", params={"mine": "true", "limit": 1}, headers=world.h["r"]).json()
    assert one["count"] == 1 and one["total"] == 2
    second = world.client.get("/api/v1/actions", params={"mine": "true", "limit": 1, "offset": 1}, headers=world.h["r"]).json()
    assert second["count"] == 1 and second["action_items"][0]["id"] != one["action_items"][0]["id"]
    patch(world, "r", "r")
    open_mine = world.client.get("/api/v1/actions", params={"mine": "true", "status": "pending,in_progress"},
                                 headers=world.h["r"]).json()
    assert open_mine["total"] == 1


def test_meetings_say_who_owns_them(world):
    c = world.client
    k = c.get("/api/v1/meetings", headers=world.h["k"]).json()["meetings"][0]
    assert k["is_owner"] is True and k["owner_name"] == "Krishna Panwala"
    r = c.get("/api/v1/meetings", headers=world.h["r"]).json()["meetings"][0]
    assert r["is_owner"] is False and r["owner_name"] == "Krishna Panwala" and r["owner_email"] == "k@example.com"
    detail = c.get(f"/api/v1/meetings/{world.mid}", headers=world.h["r"]).json()
    assert detail["is_owner"] is False and detail["owner_name"] == "Krishna Panwala"
    assert detail["can_write"] is False


def test_owner_without_a_name_has_no_owner_name(world):
    with world.Session() as db:
        u = db.query(User).filter(User.email == "k@example.com").one()
        u.name = None
        db.commit()
    r = world.client.get("/api/v1/meetings", headers=world.h["r"]).json()["meetings"][0]
    assert r["owner_name"] is None and r["owner_email"] == "k@example.com"


# ---------------------------------------------------- deadlines that need a date

def test_tasks_with_an_unclear_deadline_are_flagged_until_a_date_is_chosen(world):
    with world.Session() as db:
        t = db.get(ActionItemDB, world.ids["tbd"])
        t.assignee = "Krishna Panwala"                  # a personal list only shows the person's own tasks
        t.due_phrase = "soon"
        t.due_date = None
        db.commit()
    c = world.client
    row = lambda who: next(a for a in c.get(f"/api/v1/meetings/{world.mid}/actions", headers=world.h[who]).json()["action_items"] if a["task"] == "Book a room")
    assert row("k")["due_phrase"] == "soon" and row("k")["needs_review"] is True
    flagged = c.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["k"]).json()
    assert [a["task"] for a in flagged["action_items"]] == ["Book a room"] and flagged["action_items"][0]["can_write"] is True
    assert c.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["r"]).json()["total"] == 0   # not r's task
    # ticking it off does not clear the note, but a finished task no longer needs review
    c.patch(f"/api/v1/meetings/{world.mid}/actions/{world.ids['tbd']}", params={"status": "completed"}, headers=world.h["k"])
    assert c.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["k"]).json()["total"] == 0
    c.patch(f"/api/v1/meetings/{world.mid}/actions/{world.ids['tbd']}", params={"status": "pending"}, headers=world.h["k"])
    assert c.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["k"]).json()["total"] == 1
    r = put(world, "k", "tbd", due_date="2026-12-01T00:00:00")
    assert r.status_code == 200 and r.json()["due_phrase"] is None and r.json()["needs_review"] is False
    assert c.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["k"]).json()["total"] == 0


def test_other_edits_keep_the_note_and_clearing_a_date_forgets_it(world):
    with world.Session() as db:
        t = db.get(ActionItemDB, world.ids["tbd"])
        t.due_phrase, t.due_date = "next month", None
        db.commit()
    assert put(world, "k", "tbd", priority="high").json()["due_phrase"] == "next month"
    put(world, "k", "tbd", due_date="2026-12-01T00:00:00")
    assert put(world, "k", "tbd", clear_due_date=True).json()["due_phrase"] is None
    assert [a for a in world.client.get("/api/v1/actions", params={"needs_review": "true"}, headers=world.h["k"]).json()["action_items"]] == []


def test_a_task_with_a_date_never_shows_the_old_phrase(world):
    with world.Session() as db:
        t = db.get(ActionItemDB, world.ids["k"])
        t.due_phrase, t.due_date = "by Friday", datetime(2026, 11, 20)
        db.commit()
    row = next(a for a in world.client.get(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"]).json()["action_items"] if a["id"] == world.ids["k"])
    assert row["due_phrase"] is None and row["needs_review"] is False
