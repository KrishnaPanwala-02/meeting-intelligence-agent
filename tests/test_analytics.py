"""Analytics: who talks, who finishes tasks, how the weeks go, and who is counted."""

import uuid
from datetime import datetime, timedelta

import pytest

from src.models.database import ActionItemDB, Meeting, Transcript, User
from src.services import analytics
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401

NOW = datetime(2026, 11, 13, 12, 0)          # a Friday


def seg(i, speaker, seconds, text="one two three"):
    return {"id": i, "start_time": i * 100.0, "end_time": i * 100.0 + seconds, "speaker": speaker, "text": text}


def add(db, title, owner, *, days_ago=0, participants=(), segments=None, tasks=(), now=NOW):
    mid = str(uuid.uuid4())
    when = now - timedelta(days=days_ago)
    db.add(Meeting(id=mid, title=title, owner_email=owner, status="completed", participants=list(participants),
                   created_at=when, scheduled_time=when))
    if segments is not None:
        db.add(Transcript(id=str(uuid.uuid4()), meeting_id=mid, language="en", full_text="x", segments=segments,
                          word_count=3, speaker_count=2, created_at=when))
    for t in tasks:
        db.add(ActionItemDB(id=str(uuid.uuid4()), meeting_id=mid, task=t["task"], assignee=t.get("who"),
                            status=t.get("status", "pending"), priority=t.get("priority", "medium"),
                            due_date=t.get("due"), created_at=t.get("created", when), completed_at=t.get("done")))
    db.commit()
    return mid


@pytest.fixture
def db_user(Session):
    with Session() as db:
        db.add(User(id="u1", email="k@example.com", name="Krishna Panwala", password_hash="x", is_active=True))
        db.commit()
        yield db, db.get(User, "u1")


def people(result):
    return {p["name"]: p for p in result["people"]}


def test_talk_time_shares_words_and_attendance(db_user):
    db, k = db_user
    add(db, "A", "k@example.com", days_ago=3, segments=[
        seg(0, "Krishna Panwala", 10, "a b c d e"), seg(1, "Rahul Patel", 30, "f g h"), seg(2, "Speaker 3", 10)])
    add(db, "B", "k@example.com", days_ago=5, segments=[seg(0, "Rahul Patel", 20, "i j")])
    r = analytics.compute(db, k, 90, NOW)
    p = people(r)
    assert p["Rahul Patel"]["talk_seconds"] == 50.0 and p["Rahul Patel"]["meetings"] == 2 and p["Rahul Patel"]["words"] == 5
    assert p["Krishna Panwala"]["talk_seconds"] == 10.0 and p["Krishna Panwala"]["words"] == 5 and p["Krishna Panwala"]["meetings"] == 1
    assert p["Rahul Patel"]["talk_share"] == round(50 / 70, 4) and p["Krishna Panwala"]["talk_share"] == round(10 / 70, 4)
    assert r["overview"]["unnamed_talk_seconds"] == 10.0 and "Speaker 3" not in p
    assert [x["name"] for x in r["people"]] == ["Rahul Patel", "Krishna Panwala"]            # most talk first
    assert r["overview"]["recorded_seconds"] == (200 + 10) + 20                                 # last segment end per transcript


def test_task_completion_overdue_and_unassigned(db_user):
    db, k = db_user
    past, future = datetime(2026, 11, 10), datetime(2026, 11, 20)
    add(db, "A", "k@example.com", days_ago=3, tasks=[
        {"task": "t1", "who": "Krishna Panwala", "status": "completed", "due": past},
        {"task": "t2", "who": "Krishna Panwala", "due": past},                                  # open and late
        {"task": "t3", "who": "Rahul Patel", "due": future, "priority": "high"},
        {"task": "t4", "who": "TBD", "priority": "low"},
        {"task": "t5", "who": "Speaker 2"},
        {"task": "t6", "who": "Rahul Patel", "status": "in_progress", "due": datetime(2026, 11, 13)},   # due today: not late
    ])
    r = analytics.compute(db, k, 90, NOW)
    o, p = r["overview"], people(r)
    assert (o["tasks"], o["tasks_done"], o["tasks_open"], o["tasks_overdue"], o["tasks_unassigned_open"]) == (6, 1, 5, 1, 2)
    assert o["completion_rate"] == round(1 / 6, 4)
    k_, r_ = p["Krishna Panwala"], p["Rahul Patel"]
    assert (k_["tasks_total"], k_["tasks_done"], k_["tasks_open"], k_["tasks_overdue"], k_["completion_rate"]) == (2, 1, 1, 1, 0.5)
    assert (r_["tasks_total"], r_["tasks_done"], r_["tasks_open"], r_["tasks_overdue"], r_["completion_rate"]) == (2, 0, 2, 0, 0.0)
    assert r["open_by_priority"] == {"high": 1, "medium": 3, "low": 1}


def test_a_person_without_tasks_has_no_completion_rate(db_user):
    db, k = db_user
    add(db, "A", "k@example.com", segments=[seg(0, "Anna Larsen", 5)])
    assert people(analytics.compute(db, k, 90, NOW))["Anna Larsen"]["completion_rate"] is None


def test_spellings_of_one_person_are_one_row(db_user):
    db, k = db_user
    add(db, "A", "k@example.com", segments=[seg(0, "Rahul Patel", 10)],
        tasks=[{"task": "a", "who": "rahul patel"}, {"task": "b", "who": "RAHUL PATEL."}, {"task": "c", "who": "Rahul"}])
    r = analytics.compute(db, k, 90, NOW)
    assert [x["name"] for x in r["people"]] == ["Rahul Patel"]
    assert r["people"][0]["tasks_total"] == 3 and r["people"][0]["talk_seconds"] == 10.0


def test_a_first_name_shared_by_two_people_is_not_guessed(db_user):
    db, k = db_user
    add(db, "A", "k@example.com", segments=[seg(0, "Krishna Panwala", 10), seg(1, "Krishna Shah", 10)],
        tasks=[{"task": "a", "who": "Krishna"}])
    p = people(analytics.compute(db, k, 90, NOW))
    assert set(p) == {"Krishna Panwala", "Krishna Shah", "Krishna"}
    assert p["Krishna"]["tasks_total"] == 1 and p["Krishna Panwala"]["tasks_total"] == 0


def test_weeks_are_contiguous_mondays_with_the_right_counts(db_user):
    db, k = db_user
    # NOW is Fri 13 Nov. Mon 9 Nov..., Mon 2 Nov..., Mon 26 Oct
    add(db, "this week", "k@example.com", days_ago=1, segments=[seg(0, "Krishna Panwala", 600)],
        tasks=[{"task": "a", "who": "Krishna Panwala", "status": "completed", "created": datetime(2026, 11, 3), "done": datetime(2026, 11, 11)}])
    add(db, "last week", "k@example.com", days_ago=8)                                 # Thu 5 Nov
    add(db, "same week", "k@example.com", days_ago=9)                                 # Wed 4 Nov
    r = analytics.compute(db, k, 21, NOW)
    starts = [w["week_start"] for w in r["weeks"]]
    assert starts == ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]
    by = {w["week_start"]: w for w in r["weeks"]}
    assert by["2026-11-09"]["meetings"] == 1 and by["2026-11-09"]["talk_minutes"] == 10.0
    assert by["2026-11-02"]["meetings"] == 2 and by["2026-11-02"]["tasks_created"] == 1
    assert by["2026-11-09"]["tasks_completed"] == 1 and by["2026-10-19"]["meetings"] == 0
    assert all(set(w) == {"week_start", "meetings", "tasks_created", "tasks_completed", "talk_minutes"} for w in r["weeks"])


def test_only_the_chosen_period_counts_and_days_are_limited(db_user):
    db, k = db_user
    add(db, "recent", "k@example.com", days_ago=5)
    add(db, "old", "k@example.com", days_ago=60)
    assert analytics.compute(db, k, 30, NOW)["overview"]["meetings"] == 1
    assert analytics.compute(db, k, 90, NOW)["overview"]["meetings"] == 2
    assert analytics.compute(db, k, 3, NOW)["days"] == analytics.MIN_DAYS
    assert analytics.compute(db, k, 9999, NOW)["days"] == analytics.MAX_DAYS


def test_nothing_recorded_gives_zeros_not_errors(db_user):
    db, k = db_user
    r = analytics.compute(db, k, 30, NOW)
    assert r["overview"]["meetings"] == 0 and r["overview"]["completion_rate"] is None and r["people"] == []
    assert r["overview"]["recorded_seconds"] == 0 and len(r["weeks"]) >= 4 and all(w["meetings"] == 0 for w in r["weeks"])


# ------------------------------------------------------------------ the API

def stamp(days_ago):
    return datetime.now() - timedelta(days=days_ago)


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"), "x": signup(client, "x@example.com"),
         "admin": signup(client, "tester@example.com")}
    with Session() as db:
        add(db, "Shared", "k@example.com", days_ago=2, participants=["r@example.com"], now=datetime.now(),
            segments=[seg(0, "Krishna Panwala", 40)], tasks=[{"task": "shared task", "who": "Krishna Panwala"}])
        add(db, "Krishna only", "k@example.com", days_ago=3, now=datetime.now(),
            segments=[seg(0, "Mallory", 99)], tasks=[{"task": "private task", "who": "Mallory"}])
        add(db, "Stranger", "x@example.com", days_ago=1, now=datetime.now(),
            segments=[seg(0, "Eve", 77)], tasks=[{"task": "eve task", "who": "Eve"}])
    return type("W", (), dict(h=h, client=client))


def get(w, who, **q):
    r = w.client.get("/api/v1/analytics", params=q, headers=w.h[who])
    assert r.status_code == 200, r.text
    return r.json()


def test_api_counts_only_what_the_person_can_read(world):
    k, r, x, admin = (get(world, who) for who in ("k", "r", "x", "admin"))
    assert {p["name"] for p in k["people"]} == {"Krishna Panwala", "Mallory"} and k["overview"]["meetings"] == 2
    assert {p["name"] for p in r["people"]} == {"Krishna Panwala"} and r["overview"]["meetings"] == 1   # only the shared one
    assert "Mallory" not in str(r) and "Eve" not in str(r) and "Eve" not in str(k)
    assert {p["name"] for p in x["people"]} == {"Eve"}
    assert admin["overview"]["meetings"] == 3 and {p["name"] for p in admin["people"]} == {"Krishna Panwala", "Mallory", "Eve"}


def test_api_validation_and_sign_in(world):
    assert world.client.get("/api/v1/analytics").status_code == 401
    for days in (3, 400, 0, -5):
        assert world.client.get("/api/v1/analytics", params={"days": days}, headers=world.h["k"]).status_code == 422
    assert world.client.get("/api/v1/analytics", params={"days": "abc"}, headers=world.h["k"]).status_code == 422
    assert get(world, "k", days=7)["days"] == 7 and get(world, "k")["days"] == 90
