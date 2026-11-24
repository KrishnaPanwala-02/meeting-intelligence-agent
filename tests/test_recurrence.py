"""Repeating meetings and time zones."""

from datetime import date, datetime, timedelta

import pytest

from src.models.database import Meeting
from src.services import recurrence as rc
from src.services import scheduling, timeutil
from tests.test_security import PASSWORD, Session, client, signup  # noqa: F401


# ------------------------------------------------------------------ the rules

D = lambda *a: datetime(*a)
FRI = D(2026, 11, 13, 10, 0)


def test_weekly_biweekly_and_daily():
    assert rc.occurrences(FRI, "weekly", count=3) == [FRI, D(2026, 11, 20, 10), D(2026, 11, 27, 10)]
    assert rc.occurrences(FRI, "biweekly", count=3) == [FRI, D(2026, 11, 27, 10), D(2026, 12, 11, 10)]
    assert rc.occurrences(FRI, "daily", count=3)[-1] == D(2026, 11, 15, 10)


def test_weekdays_skip_weekends():
    got = rc.occurrences(FRI, "weekdays", count=4)
    assert [d.strftime("%a") for d in got] == ["Fri", "Mon", "Tue", "Wed"]
    sat = rc.occurrences(D(2026, 11, 14, 9), "weekdays", count=3)           # starting on a Saturday keeps that first date
    assert [d.strftime("%a") for d in sat] == ["Sat", "Mon", "Tue"]


def test_monthly_keeps_the_day_and_clamps_to_short_months():
    got = rc.occurrences(D(2026, 1, 31, 9, 30), "monthly", count=4)
    assert [(d.month, d.day) for d in got] == [(1, 31), (2, 28), (3, 31), (4, 30)]      # March is back on the 31st
    assert rc.occurrences(D(2028, 1, 31), "monthly", count=2)[1] == D(2028, 2, 29)       # leap year
    assert rc.occurrences(D(2026, 11, 13), "monthly", count=3)[2] == D(2027, 1, 13)      # across the new year


def test_the_time_of_day_never_drifts():
    assert {d.time() for d in rc.occurrences(D(2026, 3, 1, 8, 15), "weekly", count=30)} == {D(2026, 3, 1, 8, 15).time()}


def test_until_is_inclusive_and_stops_the_series():
    got = rc.occurrences(FRI, "weekly", until=date(2026, 12, 4))
    assert [d.day for d in got] == [13, 20, 27, 4]
    assert len(rc.occurrences(FRI, "daily", until=date(2026, 11, 14))) == 2


def test_limits_and_mistakes():
    assert len(rc.occurrences(FRI, "weekly", count=52)) == 52
    assert len(rc.occurrences(FRI, "daily", until=date(2030, 1, 1))) == rc.MAX_OCCURRENCES      # capped, not unlimited
    for kwargs, message in (
        ({"rule": "yearly", "count": 3}, "Repeat must be one of"),
        ({"rule": "weekly"}, "how many times"),
        ({"rule": "weekly", "count": 1}, "between 2 and 52"),
        ({"rule": "weekly", "count": 53}, "between 2 and 52"),
        ({"rule": "weekly", "until": date(2026, 11, 1)}, "before the first"),
        ({"rule": "weekly", "until": date(2026, 11, 13)}, "only one occurrence"),
    ):
        with pytest.raises(rc.RecurrenceError, match=message):
            rc.occurrences(FRI, **kwargs)


# ---------------------------------------------------------------- time zones

def test_zone_conversion_and_daylight_saving():
    assert timeutil.is_valid_tz("Asia/Kolkata") and timeutil.is_valid_tz("UTC")
    assert not timeutil.is_valid_tz("Mars/Base") and not timeutil.is_valid_tz("") and not timeutil.is_valid_tz(None)
    assert timeutil.to_utc(D(2026, 11, 13, 10), "Asia/Kolkata") == D(2026, 11, 13, 4, 30)
    assert timeutil.to_utc(D(2026, 1, 15, 9), "America/New_York") == D(2026, 1, 15, 14)      # winter: UTC-5
    assert timeutil.to_utc(D(2026, 7, 15, 9), "America/New_York") == D(2026, 7, 15, 13)      # summer: UTC-4
    assert timeutil.to_utc(D(2026, 11, 13, 10), None) == D(2026, 11, 13, 10)                 # no zone: unchanged
    assert timeutil.to_utc(D(2026, 11, 13, 10), "Bogus/Zone") == D(2026, 11, 13, 10)
    assert timeutil.from_utc(D(2026, 11, 13, 4, 30), "Asia/Kolkata") == D(2026, 11, 13, 10)
    assert timeutil.today_in("Pacific/Auckland", D(2026, 11, 13, 12)) == date(2026, 11, 14)
    assert timeutil.today_in("America/Los_Angeles", D(2026, 11, 13, 5)) == date(2026, 11, 12)


def test_a_weekly_series_keeps_its_local_time_across_the_clock_change():
    # London leaves summer time on 25 Oct 2026: 10:00 local stays 10:00 local, but is one UTC hour later
    got = rc.occurrences(D(2026, 10, 23, 10), "weekly", count=2)
    utc = [timeutil.to_utc(d, "Europe/London") for d in got]
    assert [d.hour for d in got] == [10, 10] and [d.hour for d in utc] == [9, 10]


def add(db, title, owner, when, tz, minutes=60, people=()):
    m = Meeting(id=title, title=title, owner_email=owner, status="scheduled", scheduled_time=when, timezone=tz,
                duration_minutes=minutes, participants=list(people), created_at=when)
    db.add(m)
    db.commit()
    return m


def test_conflicts_compare_real_moments_across_zones(Session):
    with Session() as db:
        add(db, "kolkata", "k@example.com", D(2026, 11, 13, 10), "Asia/Kolkata")               # 04:30-05:30 UTC
        find = lambda start, tz, minutes=60: [m.title for m in scheduling.find_conflicts(
            db, start, start + timedelta(minutes=minutes), ["k@example.com"], tz=tz)]
        assert find(D(2026, 11, 13, 10), "Europe/London") == []                  # 10:00 UTC, hours later
        assert find(D(2026, 11, 13, 5), "Europe/London") == ["kolkata"]          # 05:00 UTC overlaps
        assert find(D(2026, 11, 13, 4, 30), "UTC", 30) == ["kolkata"]
        assert find(D(2026, 11, 13, 5, 30), "UTC") == []                         # starts exactly when it ends
        assert find(D(2026, 11, 12, 23, 0), "America/Los_Angeles", 60) == []     # 07:00 UTC
        assert find(D(2026, 11, 12, 21), "America/Los_Angeles", 60) == ["kolkata"]   # 05:00 UTC


def test_meetings_without_a_zone_keep_the_old_naive_comparison(Session):
    with Session() as db:
        add(db, "plain", "k@example.com", D(2026, 11, 13, 10), None)
        hit = lambda s, tz=None: [m.title for m in scheduling.find_conflicts(db, s, s + timedelta(hours=1), ["k@example.com"])]
        assert hit(D(2026, 11, 13, 10, 30)) == ["plain"] and hit(D(2026, 11, 13, 11)) == []
        # a zone-less meeting is read in the zone of the meeting being checked
        assert [m.title for m in scheduling.find_conflicts(db, D(2026, 11, 13, 10, 30), D(2026, 11, 13, 11, 30), ["k@example.com"], tz="Asia/Kolkata")] == ["plain"]


# ------------------------------------------------------------------ the API

def future(days, hour=10, minute=0):
    return (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture
def world(client, Session):
    h = {"k": signup(client, "k@example.com"), "r": signup(client, "r@example.com"), "x": signup(client, "x@example.com")}
    client.patch("/auth/me", json={"name": "Krishna Panwala"}, headers=h["k"])
    return type("W", (), dict(h=h, client=client, Session=Session))


def schedule(w, who="k", **over):
    body = {"title": "Standup", "participants": ["r@example.com"], "scheduled_time": future(3).isoformat(), **over}
    return w.client.post("/api/v1/meetings/schedule", json=body, headers=w.h[who])


def rows(w):
    with w.Session() as db:
        return db.query(Meeting).order_by(Meeting.scheduled_time).all()


def test_a_weekly_series_creates_every_meeting_with_one_series_id(world):
    r = schedule(world, repeat="weekly", repeat_count=4)
    assert r.status_code == 201
    d = r.json()
    assert d["series"]["created"] == 4 and d["series"]["skipped"] == [] and d["series"]["recurrence"] == "weekly"
    ms = rows(world)
    assert len(ms) == 4 and len({m.series_id for m in ms}) == 1 and ms[0].series_id == d["series_id"] == d["series"]["id"]
    assert [m.scheduled_time - ms[0].scheduled_time for m in ms] == [timedelta(days=7 * i) for i in range(4)]
    assert {(m.title, m.recurrence, tuple(m.participants), m.status, m.owner_email) for m in ms} == {("Standup", "weekly", ("r@example.com",), "scheduled", "k@example.com")}


def test_a_single_meeting_is_unchanged(world):
    d = schedule(world).json()
    assert "series" not in d and d["series_id"] is None and d["recurrence"] is None and len(rows(world)) == 1


def test_repeat_until_a_date(world):
    until = (future(3) + timedelta(days=15)).date().isoformat()
    d = schedule(world, repeat="weekly", repeat_until=until).json()
    assert d["series"]["created"] == 3


def test_bad_repeat_requests(world):
    for extra in ({"repeat": "weekly"}, {"repeat": "yearly", "repeat_count": 3}, {"repeat": "weekly", "repeat_count": 53},
                  {"repeat": "weekly", "repeat_count": 1}, {"repeat_count": 3}, {"repeat_until": "2030-01-01"},
                  {"repeat": "weekly", "repeat_until": (date.today() - timedelta(days=1)).isoformat()}):
        assert schedule(world, **extra).status_code == 422, extra
    assert rows(world) == []


def test_series_use_the_users_time_zone_unless_one_is_given(world):
    world.client.patch("/auth/me", json={"timezone": "Asia/Kolkata"}, headers=world.h["k"])
    assert schedule(world).json()["timezone"] == "Asia/Kolkata"
    assert schedule(world, timezone="Europe/London", scheduled_time=future(5).isoformat()).json()["timezone"] == "Europe/London"
    assert schedule(world, timezone="Mars/Base", scheduled_time=future(6).isoformat()).status_code == 422
    assert schedule(world, who="r", scheduled_time=future(7).isoformat()).json()["timezone"] is None      # r has none set


def test_the_first_meeting_must_be_free_but_later_clashes_are_skipped_and_reported(world):
    schedule(world, title="Existing", scheduled_time=future(10, 10, 15).isoformat(), participants=["r@example.com"])    # clashes with week 2
    r = schedule(world, repeat="weekly", repeat_count=4)
    assert r.status_code == 201
    s = r.json()["series"]
    assert s["created"] == 3 and len(s["skipped"]) == 1 and s["skipped"][0]["reason"] == "Conflicts with “Existing”"
    assert s["skipped"][0]["scheduled_time"].startswith(future(10).date().isoformat())
    again = schedule(world, repeat="weekly", repeat_count=3)                  # first slot is now taken
    assert again.status_code == 409 and "conflicts" in again.json()["detail"]
    assert len(rows(world)) == 1 + 3
    allowed = schedule(world, repeat="weekly", repeat_count=3, allow_conflicts=True)
    assert allowed.status_code == 201 and allowed.json()["series"]["created"] == 3


def test_the_first_meeting_cannot_be_in_the_past_in_its_own_zone(world):
    now = datetime.utcnow().replace(microsecond=0)
    assert schedule(world, scheduled_time=(now - timedelta(hours=1)).isoformat(), timezone="UTC").status_code == 422
    # Auckland is 13 hours ahead of UTC in November. A wall-clock time 12.5 hours ahead of the server looks like
    # the future on the server's clock, but in Auckland it was 30 minutes ago, so it is refused...
    assert schedule(world, scheduled_time=(now + timedelta(hours=12, minutes=30)).isoformat(), timezone="Pacific/Auckland").status_code == 422
    # ...while 13.5 hours ahead is half an hour from now there, and accepted.
    assert schedule(world, scheduled_time=(now + timedelta(hours=13, minutes=30)).isoformat(), timezone="Pacific/Auckland").status_code == 201


def test_a_meeting_in_a_series_knows_its_place_and_siblings(world):
    ids = [m.id for m in (lambda r: (schedule(world, repeat="weekly", repeat_count=3), rows(world))[1])(None)]
    detail = lambda who, mid: world.client.get(f"/api/v1/meetings/{mid}", headers=world.h[who]).json()
    assert [detail("k", i)["series"] for i in ids] == [{"count": 3, "index": 1}, {"count": 3, "index": 2}, {"count": 3, "index": 3}]
    s = world.client.get(f"/api/v1/meetings/{ids[1]}/series", headers=world.h["k"]).json()
    assert s["recurrence"] == "weekly" and s["label"] == "every week" and [m["id"] for m in s["meetings"]] == ids
    assert [m["is_current"] for m in s["meetings"]] == [False, True, False]
    assert world.client.get(f"/api/v1/meetings/{ids[0]}/series", headers=world.h["r"]).status_code == 200          # a participant
    assert world.client.get(f"/api/v1/meetings/{ids[0]}/series", headers=world.h["x"]).status_code == 404
    single = schedule(world, scheduled_time=future(40).isoformat()).json()["id"]
    assert world.client.get(f"/api/v1/meetings/{single}/series", headers=world.h["k"]).status_code == 404
    assert "series" not in detail("k", single)


def test_cancelling_this_one_or_this_and_all_following(world):
    schedule(world, repeat="weekly", repeat_count=4)
    ids = [m.id for m in rows(world)]
    post = lambda mid, scope=None, who="k": world.client.post(f"/api/v1/meetings/{mid}/cancel", params={"scope": scope} if scope else {}, headers=world.h[who])
    r = post(ids[1])
    assert r.status_code == 200 and "cancelled_count" not in r.json()
    assert [m.status for m in rows(world)] == ["scheduled", "cancelled", "scheduled", "scheduled"]
    r = post(ids[2], "following")
    assert r.json()["cancelled_count"] == 2
    assert [m.status for m in rows(world)] == ["scheduled", "cancelled", "cancelled", "cancelled"]       # the first stays
    assert post(ids[0], "sometimes").status_code == 422
    assert post(ids[0], "following", who="r").status_code == 403
    assert post(ids[0], "following").json()["cancelled_count"] == 1                                       # nothing left to cancel after it
    single = schedule(world, scheduled_time=future(60).isoformat()).json()["id"]
    assert post(single, "following").json()["cancelled_count"] == 1


def test_editing_one_meeting_can_change_its_zone_and_is_checked_in_that_zone(world):
    mid = schedule(world, timezone="UTC").json()["id"]
    put = lambda **b: world.client.put(f"/api/v1/meetings/{mid}", json=b, headers=world.h["k"])
    r = put(timezone="Asia/Kolkata")
    assert r.status_code == 200 and r.json()["timezone"] == "Asia/Kolkata"
    assert put(timezone="Nope/Nope").status_code == 422
    assert put(title="Renamed").json()["timezone"] == "Asia/Kolkata"            # untouched when not mentioned
    assert put(timezone="").status_code == 200


def test_editing_a_meeting_into_a_clash_in_another_zone_is_refused(world):
    a = schedule(world, timezone="Asia/Kolkata", scheduled_time=future(20, 10, 0).isoformat()).json()          # 04:30 UTC
    b = schedule(world, timezone="Europe/London", scheduled_time=future(20, 12, 0).isoformat()).json()["id"]    # 12:00 UTC, no clash
    r = world.client.put(f"/api/v1/meetings/{b}", json={"scheduled_time": future(20, 5, 0).isoformat()}, headers=world.h["k"])   # 05:00 UTC
    assert r.status_code == 409 and r.json()["detail"]["conflicts"]


def test_calendar_events_carry_the_zone_and_the_real_moment(world):
    schedule(world, timezone="Asia/Kolkata", scheduled_time=future(4, 10, 0).isoformat())
    schedule(world, title="Plain", scheduled_time=future(6, 10, 0).isoformat(), participants=[])
    ev = world.client.get("/api/v1/calendar/k@example.com/events", headers=world.h["k"]).json()["events"]
    by = {e["summary"]: e for e in ev if e["type"] == "meeting"}
    kolkata = by["Standup"]
    assert kolkata["timezone"] == "Asia/Kolkata" and kolkata["start"].endswith("T10:00:00") and kolkata["start_utc"].endswith("T04:30:00Z")
    assert by["Plain"]["timezone"] is None and by["Plain"]["start_utc"] is None


def test_the_ics_file_uses_real_moments_for_meetings_with_a_zone(world):
    schedule(world, timezone="Asia/Kolkata", scheduled_time=future(4, 10, 0).isoformat(), title="Zoned")
    schedule(world, title="Floating", scheduled_time=future(6, 10, 0).isoformat(), participants=[])
    text = world.client.get("/api/v1/export/calendar.ics", headers=world.h["k"]).text
    zoned, floating = text.split("SUMMARY:Zoned")[0].rsplit("BEGIN:VEVENT", 1)[1], text.split("SUMMARY:Floating")[0].rsplit("BEGIN:VEVENT", 1)[1]
    assert "T043000Z" in zoned and "T053000Z" in zoned                       # 10:00-11:00 in Kolkata = 04:30-05:30 UTC
    assert "T100000\r\n" in floating and "Z\r\n" not in floating.split("DTSTART:")[1].split("\r\n")[0]


def test_an_uploaded_recording_can_carry_a_zone(world):
    from tests.conftest import TEST_WAV
    from src.api import main
    data = open(TEST_WAV, "rb").read()
    up = lambda **f: world.client.post("/api/v1/meetings/upload", data={"title": "Rec", **f}, files={"audio_file": ("m.wav", data)}, headers=world.h["k"])
    r = up(scheduled_time=future(2).isoformat(), timezone="Asia/Kolkata")
    assert r.status_code == 201
    with world.Session() as db:
        assert db.get(Meeting, r.json()["meeting_id"]).timezone == "Asia/Kolkata"
    assert up(scheduled_time=future(2).isoformat(), timezone="Nope/Nope").status_code == 422
    r = up(timezone="Asia/Kolkata")                                           # no scheduled time: a zone means nothing
    with world.Session() as db:
        assert db.get(Meeting, r.json()["meeting_id"]).timezone is None
