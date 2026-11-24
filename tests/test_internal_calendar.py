"""Tests for the internal calendar (isolated in-memory database)."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Meeting, ActionItemDB
from src.services import internal_calendar as cal


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def meeting(db, id, title, when, minutes=60, status="scheduled"):
    db.add(Meeting(id=id, title=title, scheduled_time=when,
                   duration_minutes=minutes, status=status,
                   participants=["a@x.com"]))


def action(db, id, mid, task, due, status="pending", **kw):
    db.add(ActionItemDB(id=id, meeting_id=mid, task=task, due_date=due,
                        status=status, **kw))


@pytest.fixture
def seeded(db):
    meeting(db, "m1", "Kickoff", datetime(2026, 11, 10, 9, 0), 30)
    meeting(db, "m2", "Review", datetime(2026, 11, 20, 14, 0))
    meeting(db, "m3", "No time set", None)
    db.get(Meeting, "m3").created_at = datetime(2026, 11, 1, 8, 0)
    action(db, "a1", "m1", "Write report", datetime(2026, 11, 12),
           assignee="Speaker 1", priority="high")
    action(db, "a2", "m1", "Train model", datetime(2026, 11, 15), "completed")
    action(db, "a3", "m2", "No due date", None)
    db.commit()
    return db


def ids(events):
    return [e["id"] for e in events]


def test_empty_calendar(db):
    assert cal.get_events(db) == []


def test_meetings_and_deadlines_sorted(seeded):
    events = cal.get_events(seeded)
    assert ids(events) == ["m3", "m1", "a1", "a2", "m2"]
    m1 = events[1]
    assert m1["end"] == "2026-11-10T09:30:00"
    assert m1["calendar_id"] == "internal"
    a1 = events[2]
    assert (a1["type"], a1["meeting_id"], a1["assignee"], a1["priority"],
            a1["meeting_title"]) == ("submission", "m1", "Speaker 1", "high",
                                     "Kickoff")


def test_existing_event_fields_preserved(seeded):
    required = {"id", "summary", "start", "end", "status", "type",
                "calendar_id", "calendar_name"}
    assert all(required <= set(e) for e in cal.get_events(seeded))


def test_date_range_filter_inclusive_whole_day(seeded):
    ev = cal.get_events(
        seeded,
        start=cal.parse_bound("2026-11-12", end=False),
        end=cal.parse_bound("2026-11-15", end=True),
    )
    assert ids(ev) == ["a1", "a2"]          # 15 Nov deadline is included


def test_meeting_overlapping_range_start_is_included(seeded):
    # m1 runs 09:00-09:30; a range starting 09:15 still overlaps it.
    ev = cal.get_events(seeded, start=datetime(2026, 11, 10, 9, 15),
                        end=datetime(2026, 11, 10, 9, 20))
    assert ids(ev) == ["m1"]


def test_filter_by_type_status_and_meeting(seeded):
    assert ids(cal.get_events(seeded, event_types=["meeting"])) == [
        "m3", "m1", "m2"]
    assert ids(cal.get_events(seeded, event_types=["submission"],
                              statuses=["completed"])) == ["a2"]
    assert ids(cal.get_events(seeded, meeting_id="m2")) == ["m2"]
    assert ids(cal.get_events(seeded, meeting_id="m1")) == ["m1", "a1", "a2"]


def test_status_filter_is_case_insensitive(seeded):
    assert ids(cal.get_events(seeded, statuses=["PENDING"])) == ["a1"]


def test_invalid_filters_raise(seeded):
    with pytest.raises(cal.CalendarFilterError):
        cal.get_events(seeded, event_types=["holiday"])
    with pytest.raises(cal.CalendarFilterError):
        cal.get_events(seeded, start=datetime(2026, 12, 1),
                       end=datetime(2026, 11, 1))
    with pytest.raises(cal.CalendarFilterError):
        cal.parse_bound("11/12/2026", end=False)


def test_duplicate_action_items_collapse_to_one_event(db):
    meeting(db, "m1", "Kickoff", datetime(2026, 11, 10, 9, 0))
    due = datetime(2026, 11, 12)
    action(db, "a1", "m1", "Write the report", due)
    action(db, "a2", "m1", "write the report!", due, "completed")
    action(db, "a3", "m1", "Write the report", datetime(2026, 11, 13))
    db.commit()
    events = [e for e in cal.get_events(db) if e["type"] == "submission"]
    assert len(events) == 2                  # same-day pair collapsed
    same_day = [e for e in events if e["start"].startswith("2026-11-12")][0]
    assert same_day["status"] == "completed"  # reflects the finished one


def test_status_update_is_reflected(seeded):
    item = seeded.get(ActionItemDB, "a1")
    item.status = "completed"
    seeded.commit()
    ev = {e["id"]: e for e in cal.get_events(seeded)}
    assert ev["a1"]["status"] == "completed"


def test_blank_tasks_skipped(db):
    meeting(db, "m1", "x", None)
    action(db, "a1", "m1", "   ", datetime(2026, 11, 12))
    db.commit()
    assert ids(cal.get_events(db)) == ["m1"]  # meeting only, blank task skipped


def test_parse_helpers():
    assert cal.parse_csv(" Pending, completed ,") == {"pending", "completed"}
    assert cal.parse_csv("") is None
    assert cal.parse_bound("2026-11-12", end=True).hour == 23
    assert cal.parse_bound("2026-11-12T10:30:00Z", end=False).hour == 10


def test_unscheduled_meeting_uses_started_then_recorded_date(db):
    meeting(db, "s", "Scheduled", datetime(2026, 11, 3, 10, 0))
    meeting(db, "st", "Started only", None)
    meeting(db, "r", "Recorded only", None)
    db.get(Meeting, "s").started_at = datetime(2026, 11, 3, 10, 5)
    db.get(Meeting, "st").started_at = datetime(2026, 11, 4, 9, 0)
    db.get(Meeting, "st").created_at = datetime(2026, 11, 9, 9, 0)
    db.get(Meeting, "r").created_at = datetime(2026, 11, 5, 9, 0)
    db.commit()
    ev = {e["id"]: e for e in cal.get_events(db)}
    assert (ev["s"]["start"], ev["s"]["date_source"]) == (
        "2026-11-03T10:00:00", "scheduled")
    assert (ev["st"]["start"], ev["st"]["date_source"]) == (
        "2026-11-04T09:00:00", "started")
    assert (ev["r"]["start"], ev["r"]["date_source"]) == (
        "2026-11-05T09:00:00", "recorded")


def test_unscheduled_meeting_respects_date_range(db):
    meeting(db, "r", "Recorded only", None)
    db.get(Meeting, "r").created_at = datetime(2026, 11, 5, 9, 0)
    db.commit()
    inside = cal.get_events(db, start=datetime(2026, 11, 5),
                            end=datetime(2026, 11, 5, 23, 59))
    outside = cal.get_events(db, start=datetime(2026, 11, 6))
    assert ids(inside) == ["r"] and outside == []
