"""Tests for internal scheduling: conflicts, free slots, health, endpoints."""

from datetime import datetime, time, timedelta, date, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from src.api.main import app
from src.models.database import Base, Meeting
from src.models.db import get_db
from src.services.auth import get_current_user
from tests.conftest import admin_override
from src.services import scheduling as sch

NOW = datetime(2026, 11, 9, 8, 0)   # a Monday, 08:00


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def add(db, id, start, minutes=60, people=("a@x.com",), status="scheduled"):
    db.add(Meeting(id=id, title=id, scheduled_time=start,
                   duration_minutes=minutes, participants=list(people),
                   status=status))
    db.commit()


# ---------------------------------------------------------------- conflicts

def test_overlap_same_participant_conflicts(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0))
    hit = sch.find_conflicts(db, datetime(2026, 11, 10, 10, 30),
                             datetime(2026, 11, 10, 11, 30), ["A@X.com"])
    assert [m.id for m in hit] == ["m1"]


def test_back_to_back_is_not_a_conflict(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0))
    assert sch.find_conflicts(db, datetime(2026, 11, 10, 11, 0),
                              datetime(2026, 11, 10, 12, 0), ["a@x.com"]) == []
    assert sch.find_conflicts(db, datetime(2026, 11, 10, 9, 0),
                              datetime(2026, 11, 10, 10, 0), ["a@x.com"]) == []


def test_different_participants_do_not_conflict(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0))
    assert sch.find_conflicts(db, datetime(2026, 11, 10, 10, 0),
                              datetime(2026, 11, 10, 11, 0), ["b@x.com"]) == []


def test_meeting_without_participants_blocks_everyone(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0), people=())
    assert len(sch.find_conflicts(db, datetime(2026, 11, 10, 10, 0),
                                  datetime(2026, 11, 10, 11, 0), ["b@x.com"])) == 1


def test_cancelled_meetings_never_conflict(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0), status="cancelled")
    assert sch.find_conflicts(db, datetime(2026, 11, 10, 10, 0),
                              datetime(2026, 11, 10, 11, 0), ["a@x.com"]) == []


def test_exclude_self_when_rescheduling(db):
    add(db, "m1", datetime(2026, 11, 10, 10, 0))
    assert sch.find_conflicts(db, datetime(2026, 11, 10, 10, 15),
                              datetime(2026, 11, 10, 11, 0), ["a@x.com"],
                              exclude_meeting_id="m1") == []


def test_long_meeting_started_yesterday_still_blocks(db):
    add(db, "m1", datetime(2026, 11, 10, 22, 0), minutes=600)  # to 08:00 next day
    assert len(sch.find_conflicts(db, datetime(2026, 11, 11, 7, 0),
                                  datetime(2026, 11, 11, 7, 30), ["a@x.com"])) == 1


def test_timezone_aware_input_converted_to_utc():
    aware = datetime(2026, 11, 10, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert sch.to_naive(aware) == datetime(2026, 11, 10, 6, 30)


# ------------------------------------------------------------- slot search

def test_best_slot_avoids_busy_time_and_respects_hours(db):
    add(db, "m1", datetime(2026, 11, 9, 9, 0), minutes=180)  # 9-12 busy
    slots = sch.suggest_slots(db, ["a@x.com"], 60, now=NOW,
                              from_date=date(2026, 11, 9), to_date=date(2026, 11, 9))
    assert slots
    for s in slots:
        assert time(9) <= s.start.time() and s.end.time() <= time(17)
        assert s.start >= datetime(2026, 11, 9, 12, 0)


def test_all_participants_must_be_free(db):
    add(db, "m1", datetime(2026, 11, 9, 9, 0), 480, people=("a@x.com",))
    add(db, "m2", datetime(2026, 11, 10, 9, 0), 60, people=("b@x.com",))
    slots = sch.suggest_slots(db, ["a@x.com", "b@x.com"], 60, now=NOW,
                              from_date=date(2026, 11, 9), to_date=date(2026, 11, 10),
                              limit=20)
    assert slots and all(s.start.date() == date(2026, 11, 10) and s.start.hour >= 10
                         for s in slots)


def test_preferred_day_wins(db):
    slots = sch.suggest_slots(db, ["a@x.com"], 30, preferred_days=["thursday"],
                              now=NOW)
    assert slots[0].start.weekday() == 3


def test_no_slots_in_the_past(db):
    slots = sch.suggest_slots(db, ["a@x.com"], 30, now=datetime(2026, 11, 9, 16, 45),
                              from_date=date(2026, 11, 9), to_date=date(2026, 11, 9))
    assert slots == []


def test_no_slot_when_fully_booked(db):
    add(db, "m1", datetime(2026, 11, 9, 9, 0), 480, people=())
    assert sch.suggest_slots(db, ["a@x.com"], 30, now=NOW,
                             from_date=date(2026, 11, 9), to_date=date(2026, 11, 9)) == []


def test_adjacent_slot_is_penalised(db):
    add(db, "m1", datetime(2026, 11, 9, 13, 0), 60)
    slots = sch.suggest_slots(db, ["a@x.com"], 60, now=NOW,
                              from_date=date(2026, 11, 9), to_date=date(2026, 11, 9),
                              limit=50)
    by_start = {s.start.hour * 60 + s.start.minute: s for s in slots}
    assert by_start[14 * 60].score < by_start[15 * 60].score


@pytest.mark.parametrize("kwargs", [
    {"duration_minutes": 1}, {"duration_minutes": 600},
    {"duration_minutes": 30, "preferred_days": ["funday"]},
    {"duration_minutes": 30, "from_date": date(2026, 11, 20), "to_date": date(2026, 11, 10)},
    {"duration_minutes": 30, "from_date": date(2026, 11, 10), "to_date": date(2027, 6, 1)},
])
def test_invalid_search_inputs(db, kwargs):
    with pytest.raises(sch.SchedulingError):
        sch.suggest_slots(db, ["a@x.com"], now=NOW, **kwargs)


def test_parse_search_window():
    assert sch.parse_search_window({"start": "10:00", "end": "12:30"}, 9, 17)[:2] == (
        time(10, 0), time(12, 30))
    with pytest.raises(sch.SchedulingError):
        sch.parse_search_window({"start": "18:00", "end": "09:00"}, 9, 17)
    with pytest.raises(sch.SchedulingError):
        sch.parse_search_window({"start": "nine"}, 9, 17)


# ------------------------------------------------------------------ health

def test_health_from_real_meetings(db):
    # Week of Mon 2 Nov: two back-to-back 1h meetings Monday, one on Tuesday
    add(db, "a", datetime(2026, 11, 2, 9, 0), 60)
    add(db, "b", datetime(2026, 11, 2, 10, 0), 60)
    add(db, "c", datetime(2026, 11, 3, 14, 0), 60)
    add(db, "other", datetime(2026, 11, 3, 15, 0), 60, people=("z@x.com",))
    add(db, "gone", datetime(2026, 11, 4, 9, 0), 60, status="cancelled")
    m = sch.calendar_metrics(db, "A@x.com", days=7, now=datetime(2026, 11, 8, 12, 0))
    assert m["meetings_analyzed"] == 3
    assert m["meeting_hours_per_week"] == 3.0
    assert m["back_to_back_meetings"] == 1
    assert m["average_meeting_duration"] == 60
    assert 0 <= m["fragmentation_score"] <= 10
    assert m["focus_time_blocks"] >= 3       # free weekdays + long gaps


def test_health_with_no_meetings(db):
    m = sch.calendar_metrics(db, "nobody@x.com", days=7, now=datetime(2026, 11, 8))
    assert m["meetings_analyzed"] == 0 and m["meeting_hours_per_week"] == 0
    assert m["fragmentation_score"] == 0.0


# --------------------------------------------------------------- endpoints

@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_current_user] = admin_override
    yield TestClient(app)
    app.dependency_overrides.clear()


def future(days=3, hour=10, minute=0):
    d = datetime.now() + timedelta(days=days)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0)


def body(when, **kw):
    return {"title": "Sync", "participants": ["a@x.com"],
            "scheduled_time": when.isoformat(), "duration_minutes": 60, **kw}


def test_schedule_conflict_update_cancel_flow(client):
    when = future()
    r = client.post("/api/v1/meetings/schedule", json=body(when))
    assert r.status_code == 201
    m1 = r.json()
    assert m1["status"] == "scheduled" and m1["end_time"].endswith(
        (when + timedelta(hours=1)).strftime("%H:%M:%S"))

    # overlapping -> 409 with details
    r = client.post("/api/v1/meetings/schedule",
                    json=body(when + timedelta(minutes=30), title="Clash"))
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["id"] == m1["id"]

    # the organizer cannot be in two places: even with different attendees the
    # same owner conflicts; allow_conflicts overrides
    assert client.post("/api/v1/meetings/schedule",
                       json=body(when, participants=["b@x.com"])).status_code == 409
    assert client.post("/api/v1/meetings/schedule",
                       json=body(when, allow_conflicts=True)).status_code == 201

    # reschedule m1 to a free time; excludes itself from the check
    new = when + timedelta(hours=3)
    r = client.put(f"/api/v1/meetings/{m1['id']}",
                   json={"scheduled_time": new.isoformat(), "title": "Renamed"})
    assert r.status_code == 200 and r.json()["title"] == "Renamed"
    assert r.json()["scheduled_time"] == new.isoformat()

    # cancel frees the slot, is idempotent, and blocks further edits
    assert client.post(f"/api/v1/meetings/{m1['id']}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/meetings/{m1['id']}/cancel").json()["already_cancelled"]
    assert client.put(f"/api/v1/meetings/{m1['id']}", json={"title": "x"}).status_code == 409
    assert client.post("/api/v1/meetings/schedule",
                       json=body(new, title="Reuse")).status_code == 201


def test_schedule_validation(client):
    assert client.post("/api/v1/meetings/schedule",
                       json=body(datetime.now() - timedelta(days=1))).status_code == 422
    assert client.post("/api/v1/meetings/schedule",
                       json=body(future(), title="")).status_code == 422
    assert client.post("/api/v1/meetings/schedule",
                       json=body(future(), duration_minutes=1)).status_code == 422
    assert client.post("/api/v1/meetings/schedule", json={"title": "x"}).status_code == 422


def test_get_list_and_not_found(client):
    ids = [client.post("/api/v1/meetings/schedule",
                       json=body(future(d), title=f"M{d}")).json()["id"]
           for d in (3, 4, 5)]
    assert client.get(f"/api/v1/meetings/{ids[0]}").json()["title"] == "M3"
    assert client.get("/api/v1/meetings/nope").status_code == 404
    assert client.put("/api/v1/meetings/nope", json={"title": "x"}).status_code == 404
    assert client.post("/api/v1/meetings/nope/cancel").status_code == 404

    listing = client.get("/api/v1/meetings").json()
    assert listing["total"] == 3
    assert [m["title"] for m in listing["meetings"]] == ["M5", "M4", "M3"]
    assert client.get("/api/v1/meetings?limit=1&offset=1").json()["meetings"][0]["title"] == "M4"
    assert client.get("/api/v1/meetings?participant=A@X.com").json()["total"] == 3
    assert client.get("/api/v1/meetings?participant=z@x.com").json()["total"] == 0
    client.post(f"/api/v1/meetings/{ids[0]}/cancel")
    assert client.get("/api/v1/meetings?status=cancelled").json()["total"] == 1
    assert client.get("/api/v1/meetings?start_date=bad").status_code == 422


def test_find_optimal_time_uses_real_calendar(client):
    # Block all of tomorrow..+13 days for a@x.com except one day.
    for d in range(1, 14):
        if d == 6:
            continue
        client.post("/api/v1/meetings/schedule",
                    json=body(future(d, 9), duration_minutes=480))
    r = client.post("/api/v1/calendar/find-optimal-time",
                    json={"participants": ["a@x.com"], "duration_minutes": 30})
    assert r.status_code == 200
    data = r.json()
    assert datetime.fromisoformat(data["optimal_time"]).date() == future(6).date()
    assert 0 < data["confidence"] <= 1 and data["reasoning"]
    assert isinstance(data["alternatives"], list)

    # another participant is unaffected -> earlier slot possible
    r2 = client.post("/api/v1/calendar/find-optimal-time",
                     json={"participants": ["free@x.com"], "duration_minutes": 30})
    assert datetime.fromisoformat(r2.json()["optimal_time"]) < datetime.fromisoformat(
        data["optimal_time"])


def test_find_optimal_time_errors(client):
    assert client.post("/api/v1/calendar/find-optimal-time",
                       json={"participants": ["a@x.com"], "duration_minutes": 2}).status_code == 422
    assert client.post("/api/v1/calendar/find-optimal-time",
                       json={"participants": ["a@x.com"], "duration_minutes": 30,
                             "preferred_days": ["funday"]}).status_code == 422
    # window entirely in the past -> nothing available
    assert client.post("/api/v1/calendar/find-optimal-time",
                       json={"participants": ["a@x.com"], "duration_minutes": 30,
                             "time_range": {"start": "09:00", "end": "09:15"}}
                       ).status_code == 404


def test_health_endpoint_and_calendar_shows_cancelled(client):
    r = client.get("/api/v1/calendar/a@x.com/health")
    assert r.status_code == 200 and r.json()["meeting_hours_per_week"] == 0
    mid = client.post("/api/v1/meetings/schedule", json=body(future())).json()["id"]
    client.post(f"/api/v1/meetings/{mid}/cancel")
    ev = client.get("/api/v1/calendar/a@x.com/events?status=cancelled").json()["events"]
    assert [e["id"] for e in ev] == [mid]


def test_owner_counts_as_attendee_for_conflicts_slots_and_health(db):
    db.add(Meeting(id="o1", title="o1", scheduled_time=datetime(2026, 11, 10, 10, 0),
                   duration_minutes=60, participants=["bob@x.com"],
                   owner_email="Alice@X.com", status="scheduled"))
    db.commit()
    assert len(sch.find_conflicts(db, datetime(2026, 11, 10, 10, 0),
                                  datetime(2026, 11, 10, 11, 0), ["alice@x.com"])) == 1
    slots = sch.suggest_slots(db, ["alice@x.com"], 60, now=datetime(2026, 11, 9, 8, 0),
                              from_date=date(2026, 11, 10), to_date=date(2026, 11, 10), limit=40)
    assert all(not (s.start.hour == 10) for s in slots)
    m = sch.calendar_metrics(db, "alice@x.com", days=7, now=datetime(2026, 11, 12))
    assert m["meetings_analyzed"] == 1
