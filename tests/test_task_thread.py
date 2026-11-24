"""Comments on tasks and requests for more time."""

import uuid
from datetime import date, datetime, timedelta

import pytest

from src.models.database import ActionItemDB, Meeting, Notification, TaskComment
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

TODAY = date.today()
DUE = datetime.combine(TODAY + timedelta(days=3), datetime.min.time())
LATER = (TODAY + timedelta(days=10)).isoformat() + "T00:00:00"


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"),
         "a": signup(client, "a@example.com"), "x": signup(client, "x@example.com")}
    for who, name in (("k", "Krishna Panwala"), ("r", "Rahul Patel"), ("a", "Anna Larsen"), ("x", "Stranger")):
        client.patch("/auth/me", json={"name": name}, headers=h[who])
    mid = str(uuid.uuid4())
    ids = {}
    with Session() as db:
        db.add(Meeting(id=mid, title="ZOMATO", owner_email="k@example.com", status="uploaded",
                       participants=["r@example.com", "a@example.com"], created_at=datetime.utcnow()))
        for key, who, due in (("rahul", "Rahul Patel", DUE), ("krishna", "Krishna Panwala", DUE), ("nodate", "Rahul Patel", None)):
            ids[key] = str(uuid.uuid4())
            db.add(ActionItemDB(id=ids[key], meeting_id=mid, task=f"{key} task", assignee=who, status="pending",
                                priority="medium", due_date=due))
        db.commit()
    return type("W", (), dict(h=h, mid=mid, ids=ids, client=client, Session=Session))


def url(w, key, tail=""):
    return f"/api/v1/meetings/{w.mid}/actions/{w.ids[key]}{tail}"


def comment(w, who, body, key="rahul"):
    return w.client.post(url(w, key, "/comments"), json={"body": body}, headers=w.h[who])


def thread(w, who, key="rahul"):
    r = w.client.get(url(w, key, "/thread"), headers=w.h[who])
    assert r.status_code == 200, r.text
    return r.json()


def inbox(w, who):
    return [(n["kind"], n["title"], n["body"]) for n in w.client.get("/api/v1/notifications", headers=w.h[who]).json()["items"]]


# ----------------------------------------------------------------- comments

def test_readers_can_discuss_a_task(world):
    r = comment(world, "r", "  I need   the data first.\n\n\n\n\nCan you send it?  ")
    assert r.status_code == 201
    c = r.json()
    assert c["kind"] == "comment" and c["body"] == "I need the data first.\n\nCan you send it?" and c["mine"] is True
    assert c["author"] == {"id": c["author"]["id"], "name": "Rahul Patel", "email": "r@example.com"}
    comment(world, "k", "Sending now.")
    comment(world, "a", "Same question from me.")
    t = thread(world, "k")
    assert [x["body"] for x in t["comments"]] == ["I need the data first.\n\nCan you send it?", "Sending now.", "Same question from me."]
    assert [x["mine"] for x in t["comments"]] == [False, True, False]
    assert t["can_comment"] is True


def test_strangers_cannot_see_or_join_the_discussion(world):
    assert comment(world, "x", "hello").status_code == 404
    assert world.client.get(url(world, "rahul", "/thread"), headers=world.h["x"]).status_code == 404
    assert world.client.post(url(world, "rahul", "/comments"), json={"body": "x"}).status_code == 401
    assert world.client.get(url(world, "rahul", "/thread")).status_code == 401
    assert world.client.get(f"/api/v1/meetings/{world.mid}/actions/nope/thread", headers=world.h["k"]).status_code == 404


def test_comment_text_is_checked(world):
    assert comment(world, "r", "   ").status_code == 422
    assert comment(world, "r", "x" * 1001).status_code == 422
    assert comment(world, "r", "bad\x00text").status_code == 422
    assert comment(world, "r", "x" * 2001).status_code == 422
    assert world.client.post(url(world, "rahul", "/comments"), json={}, headers=world.h["r"]).status_code == 422
    assert comment(world, "r", "x" * 1000).status_code == 201


def test_deleting_comments(world):
    mine = comment(world, "r", "mine").json()["id"]
    theirs = comment(world, "a", "anna's").json()["id"]
    d = lambda who, cid: world.client.delete(url(world, "rahul", f"/comments/{cid}"), headers=world.h[who]).status_code
    assert d("r", theirs) == 403                         # not yours
    assert d("x", mine) == 404
    assert d("r", mine) == 204 and d("r", mine) == 404
    assert d("k", theirs) == 204                         # the meeting owner may remove any comment
    assert thread(world, "k")["comments"] == []


def test_comments_notify_the_people_involved_but_not_the_author(world):
    comment(world, "r", "Question about this")
    assert [n[0] for n in inbox(world, "k")] == ["task_comment"]           # the owner
    assert inbox(world, "a") == []                                         # a participant not involved yet
    assert inbox(world, "r") == []
    comment(world, "a", "Me too")
    assert any(n[0] == "task_comment" and "Anna Larsen: Me too" in n[2] for n in inbox(world, "r"))   # the assignee
    comment(world, "k", "Answering")
    assert any("Krishna Panwala: Answering" in n[2] for n in inbox(world, "a"))                     # earlier commenters
    title = inbox(world, "k")[0]
    assert title[1] == "New comment: rahul task"


def test_the_list_shows_comment_counts_and_open_requests(world):
    comment(world, "r", "a")
    comment(world, "k", "b")
    world.client.post(url(world, "rahul", "/extension"), json={"requested_due": LATER}, headers=world.h["r"])
    row = next(a for a in world.client.get(f"/api/v1/meetings/{world.mid}/actions", headers=world.h["k"]).json()["action_items"] if a["id"] == world.ids["rahul"])
    assert row["comment_count"] == 3 and row["pending_extension"]["requested_due"] == LATER[:10]
    assert row["pending_extension"]["by"]["name"] == "Rahul Patel"
    other = next(a for a in world.client.get("/api/v1/actions", headers=world.h["k"]).json()["action_items"] if a["id"] == world.ids["krishna"])
    assert other["comment_count"] == 0 and other["pending_extension"] is None


# ------------------------------------------------------------ extension requests

def ask(w, who, due=LATER, reason="Waiting on the data", key="rahul"):
    return w.client.post(url(w, key, "/extension"), json={"requested_due": due, "reason": reason}, headers=w.h[who])


def decide(w, who, rid, approve, note=None, key="rahul"):
    return w.client.post(url(w, key, f"/extension/{rid}/decision"), json={"approve": approve, "note": note}, headers=w.h[who])


def test_the_assignee_asks_and_the_owner_is_told(world):
    r = ask(world, "r")
    assert r.status_code == 201
    req = r.json()
    assert req["kind"] == "extension_request" and req["status"] == "pending" and req["requested_due"] == LATER[:10]
    assert req["body"] == "Waiting on the data"
    n = [x for x in inbox(world, "k") if x[0] == "extension_requested"]
    assert len(n) == 1 and n[0][1] == "More time asked: rahul task" and "Rahul Patel asks to move the deadline to" in n[0][2]
    assert "Waiting on the data" in n[0][2]
    assert inbox(world, "a") == [] and [x for x in inbox(world, "r") if x[0] == "extension_requested"] == []


def test_only_the_assignee_may_ask_and_only_one_request_waits_at_a_time(world):
    assert ask(world, "a").status_code == 403                  # a participant it is not assigned to
    assert ask(world, "k").status_code == 403                  # the owner edits the date directly
    assert ask(world, "x").status_code == 404
    assert ask(world, "r").status_code == 201
    assert ask(world, "r").status_code == 409
    assert thread(world, "r")["can_request_extension"] is False and thread(world, "a")["can_request_extension"] is False


def test_the_requested_date_must_make_sense(world):
    assert ask(world, "r", due=(TODAY + timedelta(days=3)).isoformat() + "T00:00:00").status_code == 422     # not later than the deadline
    assert ask(world, "r", due=(TODAY + timedelta(days=1)).isoformat() + "T00:00:00").status_code == 422
    assert ask(world, "r", due=(TODAY - timedelta(days=2)).isoformat() + "T00:00:00").status_code == 422
    assert ask(world, "r", due="not a date").status_code == 422
    assert ask(world, "r", reason="x" * 1001).status_code == 422
    assert ask(world, "r", due=(TODAY + timedelta(days=4)).isoformat() + "T00:00:00", reason="").status_code == 201
    r = ask(world, "r", key="nodate", reason="", due=(TODAY + timedelta(days=1)).isoformat() + "T00:00:00")   # no current deadline: any future date
    assert r.status_code == 201 and r.json()["body"] is None


def test_no_request_for_a_finished_task(world):
    world.client.patch(url(world, "rahul"), params={"status": "completed"}, headers=world.h["r"])
    r = ask(world, "r")
    assert r.status_code == 409 and "already done" in r.json()["detail"]


def test_approving_moves_the_deadline_and_tells_the_requester(world):
    rid = ask(world, "r").json()["id"]
    r = decide(world, "k", rid, True, "Fine, but no later.")
    assert r.status_code == 200
    d = r.json()
    assert d["due_date"][:10] == LATER[:10] and d["due_phrase"] is None
    assert d["pending_extension"] is None and d["can_decide"] is True and d["can_request_extension"] is False   # seen by the owner
    done = d["comments"][0]
    assert done["status"] == "approved" and done["decided_by"]["name"] == "Krishna Panwala" and done["decision_note"] == "Fine, but no later."
    n = [x for x in inbox(world, "r") if x[0] == "extension_decided"]
    assert n[0][1] == "Request approved: rahul task" and "Krishna Panwala approved: the new deadline is" in n[0][2] and "Fine, but no later." in n[0][2]


def test_approving_an_unclear_deadline_clears_the_review_flag(world):
    with world.Session() as db:
        t = db.get(ActionItemDB, world.ids["nodate"])
        t.due_phrase = "soon"
        db.commit()
    rid = ask(world, "r", key="nodate", due=(TODAY + timedelta(days=2)).isoformat() + "T00:00:00").json()["id"]
    d = decide(world, "k", rid, True, key="nodate").json()
    assert d["needs_review"] is False and d["due_phrase"] is None


def test_declining_keeps_the_deadline_and_allows_a_new_request(world):
    rid = ask(world, "r").json()["id"]
    d = decide(world, "k", rid, False, "Not possible.").json()
    assert d["due_date"][:10] == DUE.date().isoformat()
    assert d["comments"][0]["status"] == "declined"
    assert [x for x in inbox(world, "r") if x[0] == "extension_decided"][0][1] == "Request declined: rahul task"
    assert ask(world, "r", due=(TODAY + timedelta(days=5)).isoformat() + "T00:00:00").status_code == 201


def test_only_the_owner_decides_and_only_once(world):
    rid = ask(world, "r").json()["id"]
    assert decide(world, "r", rid, True).status_code == 403          # you cannot approve your own request
    assert decide(world, "a", rid, True).status_code == 403
    assert decide(world, "x", rid, True).status_code == 404
    assert decide(world, "k", "no-such-request", True).status_code == 404
    assert decide(world, "k", rid, False).status_code == 200
    assert decide(world, "k", rid, True).status_code == 409
    assert decide(world, "k", rid, True, note="x" * 1001).status_code in (409, 422)
    assert world.client.post(url(world, "rahul", f"/extension/{rid}/decision"), json={"approve": True}).status_code == 401
    assert world.client.post(url(world, "rahul", f"/extension/{rid}/decision"), json={}, headers=world.h["k"]).status_code == 422


def test_a_request_for_another_task_cannot_be_decided_here(world):
    rid = ask(world, "r").json()["id"]
    assert decide(world, "k", rid, True, key="krishna").status_code == 404


def test_requests_cannot_be_deleted_like_comments(world):
    rid = ask(world, "r").json()["id"]
    assert world.client.delete(url(world, "rahul", f"/comments/{rid}"), headers=world.h["k"]).status_code == 409


def test_the_thread_says_what_each_person_may_do(world):
    t = thread(world, "r")
    assert (t["can_comment"], t["can_request_extension"], t["can_decide"]) == (True, True, False)
    t = thread(world, "k")
    assert (t["can_comment"], t["can_request_extension"], t["can_decide"]) == (True, False, True)
    assert thread(world, "a")["can_request_extension"] is False


def test_deleting_a_task_or_meeting_removes_its_discussion(world):
    comment(world, "r", "hello")
    ask(world, "r")
    assert world.client.delete(url(world, "rahul"), headers=world.h["k"]).status_code == 200
    with world.Session() as db:
        assert db.query(TaskComment).count() == 0
    comment(world, "r", "on another task", key="nodate")
    assert world.client.delete(f"/api/v1/meetings/{world.mid}", headers=world.h["k"]).status_code == 204
    with world.Session() as db:
        assert db.query(TaskComment).count() == 0 and db.query(Notification).filter(Notification.meeting_id == world.mid).count() == 0
