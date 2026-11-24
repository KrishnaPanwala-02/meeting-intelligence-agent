"""
End-to-end workflow against a RUNNING deployment (real worker, Redis,
Postgres, Whisper, Pyannote, Ollama). Skipped unless E2E_BASE_URL is set:

    E2E_BASE_URL=http://localhost:8000 pytest tests/e2e -m e2e

It creates a fresh user and meeting each run, so point it at a disposable
deployment (see `make verify-deploy`), not at data you care about.
"""

import os
import time
import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest

from tests.conftest import TEST_WAV

pytestmark = pytest.mark.e2e

BASE = os.getenv("E2E_BASE_URL")
PASSWORD = "e2e-passphrase-12345"


@pytest.fixture(scope="module")
def http():
    if not BASE:
        pytest.skip("E2E_BASE_URL not set")
    with httpx.Client(base_url=BASE, timeout=60) as c:
        yield c


def signup(http, name):
    email = f"{name}-{uuid.uuid4().hex[:8]}@e2e.test"
    assert http.post("/auth/register", json={"email": email, "password": PASSWORD}).status_code == 201
    t = http.post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    return email, t, {"Authorization": f"Bearer {t['access_token']}"}


def next_weekday(ref: date, weekday: int) -> date:
    ahead = (weekday - ref.weekday()) % 7 or 7
    return ref + timedelta(days=ahead)


@pytest.fixture(scope="module")
def world(http):
    w = type("W", (), {})()
    w.owner_email, w.owner_tokens, w.owner = signup(http, "owner")
    w.bob_email, _, w.bob = signup(http, "bob")
    _, _, w.carol = signup(http, "carol")
    return w


def test_01_service_is_up_and_ready(http):
    assert http.get("/health").json()["status"] == "healthy"
    ready = http.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok" and ready.json()["checks"]["redis"] == "ok"


def test_02_api_is_locked_down(http):
    assert http.get("/api/v1/meetings").status_code == 401
    assert http.get("/api/v1/calendar/x@y.com/events").status_code == 401
    pre = http.options("/api/v1/meetings", headers={"Origin": "http://evil.example",
                                                    "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in pre.headers


def test_03_upload_recording(http, world):
    with open(TEST_WAV, "rb") as f:
        r = http.post("/api/v1/meetings/upload", headers=world.owner,
                      data={"title": "E2E planning", "participants": world.bob_email},
                      files={"audio_file": ("two_speakers.wav", f, "audio/wav")})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "uploaded" and d["audio_duration_seconds"] == pytest.approx(6.3, abs=0.3)
    world.mid = d["meeting_id"]
    world.created = datetime.fromisoformat(d["created_at"]).date()


def test_04_background_processing_completes(http, world):
    r = http.post(f"/api/v1/meetings/{world.mid}/process", headers=world.owner)
    assert r.status_code == 202, r.text
    job = r.json()["job_id"]
    # a second submit while running is rejected
    assert http.post(f"/api/v1/meetings/{world.mid}/process", headers=world.owner).status_code in (409, 202)
    deadline, state = time.time() + 420, None
    while time.time() < deadline:
        s = http.get(f"/api/v1/jobs/{job}", headers=world.owner).json()
        state = s["state"]
        if state in ("succeeded", "failed"):
            break
        time.sleep(3)
    assert state == "succeeded", s
    assert [x["state"] for x in s["steps"]] == ["succeeded"] * 3 and s["progress_percent"] == 100
    assert http.get(f"/api/v1/meetings/{world.mid}/jobs/latest", headers=world.owner).json()["state"] == "succeeded"


def test_05_results_are_retrievable_and_correct(http, world):
    t = http.get(f"/api/v1/meetings/{world.mid}/transcript", headers=world.owner).json()
    text = t["transcript"].lower()
    assert "model training" in text and "project report" in text
    assert t["speaker_count"] == 2
    assert [s["speaker"] for s in t["segments"]][:2] == ["Speaker 1", "Speaker 2"]

    sm = http.get(f"/api/v1/meetings/{world.mid}/summary", headers=world.owner).json()
    assert len(sm["executive_summary"]) > 30 and sm["key_points"]

    acts = http.get(f"/api/v1/meetings/{world.mid}/actions", headers=world.owner).json()["action_items"]
    by = {a["task"].lower(): a for a in acts}
    training = next(a for k, a in by.items() if "training" in k)
    report = next(a for k, a in by.items() if "report" in k)
    assert (training["assignee"], report["assignee"]) == ("Speaker 1", "Speaker 2")
    # deadlines are resolved in Python relative to the meeting date
    assert training["due_date"][:10] == next_weekday(world.created, 4).isoformat()   # "by Friday"
    assert report["due_date"][:10] == next_weekday(world.created, 0).isoformat()     # "by Monday"
    world.training_id, world.report_id = training["id"], report["id"]


def test_06_access_control_between_real_users(http, world):
    p = f"/api/v1/meetings/{world.mid}"
    assert http.get(p, headers=world.bob).status_code == 200                      # participant reads
    assert http.get(p + "/transcript", headers=world.bob).status_code == 200
    assert http.post(p + "/process", headers=world.bob).status_code == 403          # ...but cannot write
    assert http.patch(f"{p}/actions/{world.report_id}?status=completed", headers=world.bob).status_code == 403
    for suffix in ("", "/transcript", "/summary", "/actions"):
        assert http.get(p + suffix, headers=world.carol).status_code == 404         # stranger sees nothing
    assert http.get("/api/v1/meetings", headers=world.carol).json()["total"] == 0


def test_07_task_lifecycle_updates_the_internal_calendar(http, world):
    p = f"/api/v1/meetings/{world.mid}/actions/{world.training_id}"
    assert http.patch(p + "?status=completed", headers=world.owner).json()["status"] == "completed"
    cal = http.get(f"/api/v1/calendar/{world.owner_email}/events", headers=world.owner).json()
    ev = {e["id"]: e for e in cal["events"]}
    assert cal["calendar_id"] == "internal"
    assert ev[world.training_id]["status"] == "completed" and ev[world.training_id]["type"] == "submission"
    assert ev[world.report_id]["status"] == "pending"
    assert ev[world.mid]["type"] == "meeting" and ev[world.mid]["date_source"] == "recorded"
    # calendars are private
    assert http.get(f"/api/v1/calendar/{world.owner_email}/events", headers=world.carol).status_code == 403
    only_done = http.get(f"/api/v1/calendar/{world.owner_email}/events?status=completed&event_type=submission",
                         headers=world.owner).json()
    assert [e["id"] for e in only_done["events"]] == [world.training_id]


def test_08_scheduling_conflicts_and_optimization(http, world):
    when = (datetime.now() + timedelta(days=5)).replace(hour=10, minute=0, second=0, microsecond=0)
    body = {"title": "Design review", "participants": [world.bob_email],
            "scheduled_time": when.isoformat(), "duration_minutes": 60}
    first = http.post("/api/v1/meetings/schedule", headers=world.owner, json=body)
    assert first.status_code == 201
    mid = first.json()["id"]
    clash = http.post("/api/v1/meetings/schedule", headers=world.owner, json={**body, "title": "Clash"})
    assert clash.status_code == 409 and clash.json()["detail"]["conflicts"][0]["id"] == mid
    slot = http.post("/api/v1/calendar/find-optimal-time", headers=world.owner, json={
        "participants": [world.bob_email, world.owner_email], "duration_minutes": 60}).json()
    start = datetime.fromisoformat(slot["optimal_time"])
    assert not (when <= start < when + timedelta(hours=1)) and slot["reasoning"]
    moved = http.put(f"/api/v1/meetings/{mid}", headers=world.owner,
                     json={"scheduled_time": (when + timedelta(hours=3)).isoformat()})
    assert moved.status_code == 200
    assert http.post(f"/api/v1/meetings/{mid}/cancel", headers=world.owner).json()["status"] == "cancelled"
    cancelled = http.get(f"/api/v1/calendar/{world.owner_email}/events?status=cancelled", headers=world.owner).json()
    assert [e["id"] for e in cancelled["events"]] == [mid]


def test_09_token_lifecycle(http, world):
    t = world.owner_tokens
    new = http.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
    assert new["refresh_token"] != t["refresh_token"]
    # replaying the consumed refresh token is treated as theft
    assert http.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert http.get("/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 401
    fresh = http.post("/auth/login", json={"email": world.owner_email, "password": PASSWORD}).json()
    h = {"Authorization": f"Bearer {fresh['access_token']}"}
    assert http.get("/auth/me", headers=h).status_code == 200
    assert http.post("/auth/logout", headers=h).status_code == 204
    assert http.get("/auth/me", headers=h).status_code == 401
