"""API integration: the main user flows over HTTP (isolated DB, real audio,
fake AI agents). Authentication is exercised in test_security/test_sessions."""

import http.server
import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agent.action_extractor import ActionExtractionError
from src.agent.summarizer import SummarizationError
from src.agent.transcriber import (DiarizationUnavailableError,
                                   TranscriptionUnavailableError)
from src.api import main
from src.models.database import ActionItemDB, Base, Meeting, Transcript
from src.models.db import get_db
from src.services.auth import get_current_user
from tests.conftest import TEST_WAV, admin_override
from tests.test_celery_pipeline import (FakeExtractor, FakeSummarizer,
                                        FakeTranscriber)

app = main.app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path / "audio"))
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
    e = type("E", (), {})()
    e.Session, e.client, e.tmp = Session, TestClient(app), tmp_path
    e.transcriber, e.summarizer, e.extractor = FakeTranscriber(), FakeSummarizer(), FakeExtractor()
    monkeypatch.setattr(main, "transcriber", e.transcriber)
    monkeypatch.setattr(main, "summarizer", e.summarizer)
    monkeypatch.setattr(main, "action_extractor", e.extractor)
    yield e
    app.dependency_overrides.clear()


def upload(c, name="meeting.wav", data=None, **form):
    data = open(TEST_WAV, "rb").read() if data is None else data
    form = {"title": "Weekly sync", "participants": "a@x.com, b@x.com", **form}
    return c.post("/api/v1/meetings/upload", data=form, files={"audio_file": (name, data)})


# ------------------------------------------------------------------ upload

def test_upload_without_a_duration_uses_the_real_recording_length(env):
    d = upload(env.client).json()                       # 6.3 s of audio
    s = env.Session()
    assert s.get(Meeting, d["meeting_id"]).duration_minutes == 1       # not the old 60 default
    detail = env.client.get(f"/api/v1/meetings/{d['meeting_id']}").json()
    assert detail["audio_seconds"] == pytest.approx(6.3, abs=0.2)
    assert detail["duration_minutes"] == 1


def test_detail_survives_a_missing_audio_file(env):
    d = upload(env.client).json()
    s = env.Session()
    m = s.get(Meeting, d["meeting_id"])
    import os
    os.remove(m.audio_file_path)
    detail = env.client.get(f"/api/v1/meetings/{d['meeting_id']}")
    assert detail.status_code == 200 and detail.json()["audio_seconds"] is None


def test_upload_rejects_a_zero_duration(env):
    assert upload(env.client, duration_minutes="0").status_code == 422


def test_upload_creates_meeting_and_stores_audio(env):
    r = upload(env.client, duration_minutes="45")
    assert r.status_code == 201
    d = r.json()
    assert d["status"] == "uploaded" and d["participants"] == ["a@x.com", "b@x.com"]
    assert d["audio_duration_seconds"] == pytest.approx(6.3, abs=0.2)
    assert d["audio_size_bytes"] == len(open(TEST_WAV, "rb").read())
    s = env.Session()
    m = s.get(Meeting, d["meeting_id"])
    assert m.owner_email == "tester@example.com" and m.duration_minutes == 45
    assert m.audio_original_filename == "meeting.wav"
    import os
    assert os.path.exists(m.audio_file_path)
    s.close()
    assert env.client.get(f"/api/v1/meetings/{d['meeting_id']}").json()["has_audio"] is True


@pytest.mark.parametrize("kwargs,code", [
    (dict(name="notes.txt", data=b"hello"), 415),
    (dict(name="empty.wav", data=b""), 400),
    (dict(name="corrupt.wav", data=b"\x00\x01" * 3000), 400),
    (dict(name="fake.wav", data=b"this is text pretending to be audio"), 400),
])
def test_upload_rejects_bad_files(env, kwargs, code):
    assert upload(env.client, **kwargs).status_code == code
    assert env.Session().query(Meeting).count() == 0           # no orphan rows


def test_upload_validation_and_oversize(env, monkeypatch):
    assert env.client.post("/api/v1/meetings/upload",
                           files={"audio_file": ("a.wav", open(TEST_WAV, "rb").read())}).status_code == 422
    assert upload(env.client, title="x" * 201).status_code == 422
    assert upload(env.client, duration_minutes="0").status_code == 422
    monkeypatch.setenv("MAX_AUDIO_SIZE_MB", "0.01")
    assert upload(env.client).status_code == 413


def test_upload_sanitizes_hostile_filename(env):
    d = upload(env.client, name="../../etc/evil.wav").json()
    assert d["audio_filename"] == "evil.wav"
    path = env.Session().get(Meeting, d["meeting_id"]).audio_file_path
    assert str(env.tmp / "audio") in path and ".." not in path


def test_upload_db_failure_removes_stored_file(env, monkeypatch):
    from sqlalchemy.orm import Session as SA

    def boom(self, *a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(SA, "commit", boom)
    r = upload(env.client)
    assert r.status_code == 500 and "db down" not in r.text
    import os
    root = env.tmp / "audio"
    assert not root.exists() or not any(root.rglob("*.wav"))


# ---------------------------------------------------------------- transcribe

def mk_meeting(env, **kw):
    return upload(env.client, **kw).json()["meeting_id"]


def test_transcribe_stored_audio_then_fetch(env):
    mid = mk_meeting(env)
    r = env.client.post(f"/api/v1/meetings/{mid}/transcribe?identify_speakers=true&language=en")
    assert r.status_code == 200
    d = r.json()
    assert d["speaker_count"] == 2 and d["word_count"] == 14 and len(d["segments"]) == 2
    assert d["saved_to_database"] is True
    got = env.client.get(f"/api/v1/meetings/{mid}/transcript").json()
    assert got["transcript_id"] == d["transcript_id"]
    assert got["segments"][0]["speaker"] == "Speaker 1" and got["language"] == "en"
    assert "finish the report" in got["transcript"]
    s = env.Session()
    assert s.get(Transcript, d["transcript_id"]).audio_file_path.endswith(".wav")
    s.close()


def test_transcribe_attached_file_is_not_persisted_as_path(env):
    s = env.Session()
    s.add(Meeting(id="m1", title="t", owner_email="tester@example.com"))
    s.commit(); s.close()
    r = env.client.post("/api/v1/meetings/m1/transcribe",
                        files={"audio_file": ("a.wav", open(TEST_WAV, "rb").read())})
    assert r.status_code == 200
    assert env.Session().query(Transcript).one().audio_file_path is None
    r = env.client.post("/api/v1/meetings/m1/transcribe", files={"audio_file": ("a.wav", b"")})
    assert r.status_code == 400


def test_transcribe_errors(env, monkeypatch):
    s = env.Session(); s.add(Meeting(id="noaudio", title="t")); s.commit(); s.close()
    assert env.client.post("/api/v1/meetings/noaudio/transcribe").status_code == 400
    assert env.client.post("/api/v1/meetings/ghost/transcribe").status_code == 404
    mid = mk_meeting(env)
    assert env.client.post(f"/api/v1/meetings/{mid}/transcribe?language=english!").status_code == 422
    for exc, code in [(TranscriptionUnavailableError("whisper missing"), 503),
                      (DiarizationUnavailableError("needs HF_TOKEN"), 503),
                      (RuntimeError("secret internals"), 500)]:
        def boom(**k): raise exc
        monkeypatch.setattr(env.transcriber, "transcribe", boom)
        r = env.client.post(f"/api/v1/meetings/{mid}/transcribe")
        assert r.status_code == code
        assert "secret" not in r.text
    assert env.Session().query(Transcript).count() == 0         # nothing half-saved


def test_get_transcript_missing(env):
    s = env.Session(); s.add(Meeting(id="m", title="t")); s.commit(); s.close()
    assert env.client.get("/api/v1/meetings/m/transcript").status_code == 404


def test_transcript_survives_missing_audio_file_on_disk(env):
    mid = mk_meeting(env)
    import os
    os.remove(env.Session().get(Meeting, mid).audio_file_path)
    assert env.client.post(f"/api/v1/meetings/{mid}/transcribe").status_code == 400


# ----------------------------------------------------------------- summarize

def with_transcript(env):
    mid = mk_meeting(env)
    env.client.post(f"/api/v1/meetings/{mid}/transcribe?identify_speakers=true")
    return mid


def test_summarize_save_cache_regenerate_and_fetch(env):
    mid = with_transcript(env)
    assert env.client.get(f"/api/v1/meetings/{mid}/summary").status_code == 404
    r = env.client.post(f"/api/v1/meetings/{mid}/summarize")
    assert r.status_code == 200
    d = r.json()
    assert d["executive_summary"] == "Two commitments." and d["meeting_title"] == "Weekly sync"
    assert d["key_points"][0]["topic"] == "Report" and d["saved_to_database"] is True
    again = env.client.post(f"/api/v1/meetings/{mid}/summarize").json()
    assert again["summary_id"] == d["summary_id"] and again["already_existed"] is True
    assert env.summarizer.calls == 1
    regen = env.client.post(f"/api/v1/meetings/{mid}/summarize?regenerate=true").json()
    assert regen["summary_id"] != d["summary_id"] and "already_existed" not in regen
    assert env.client.get(f"/api/v1/meetings/{mid}/summary").json()["summary_id"] == regen["summary_id"]


def test_summarize_errors(env):
    assert env.client.post("/api/v1/meetings/ghost/summarize").status_code == 404
    s = env.Session(); s.add(Meeting(id="nt", title="t")); s.commit(); s.close()
    assert env.client.post("/api/v1/meetings/nt/summarize").status_code == 404
    s = env.Session()
    s.add(Meeting(id="empty", title="t")); s.add(Transcript(id="t", meeting_id="empty", segments=[]))
    s.commit(); s.close()
    assert env.client.post("/api/v1/meetings/empty/summarize").status_code == 400
    mid = with_transcript(env)
    env.summarizer.failures = 99
    r = env.client.post(f"/api/v1/meetings/{mid}/summarize")
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]
    from src.models.database import Summary
    assert env.Session().query(Summary).count() == 0


# ------------------------------------------------------------- extract/actions

def test_extract_then_dedup_then_unresolved_deadlines(env):
    mid = with_transcript(env)
    d = env.client.post(f"/api/v1/meetings/{mid}/extract-actions").json()
    assert d["count"] == 2 and d["skipped_duplicates"] == 0
    assert d["unresolved_deadlines"] == [{"task": "Book the room", "due_date_phrase": "soon"}]
    by = {a["task"]: a for a in d["action_items"]}
    assert by["Finish the report"]["due_date"] == "2026-11-13T00:00:00"
    assert by["Finish the report"]["priority"] == "high"
    again = env.client.post(f"/api/v1/meetings/{mid}/extract-actions").json()
    assert again["count"] == 0 and again["skipped_duplicates"] == 2
    listing = env.client.get(f"/api/v1/meetings/{mid}/actions").json()
    assert listing["count"] == 2 and listing["meeting_id"] == mid


def test_extract_errors(env):
    assert env.client.post("/api/v1/meetings/ghost/extract-actions").status_code == 404
    s = env.Session(); s.add(Meeting(id="nt", title="t")); s.commit(); s.close()
    assert env.client.post("/api/v1/meetings/nt/extract-actions").status_code == 404
    mid = with_transcript(env)
    env.extractor.failures = 99
    assert env.client.post(f"/api/v1/meetings/{mid}/extract-actions").status_code == 503
    assert env.Session().query(ActionItemDB).count() == 0


def test_action_item_lifecycle(env):
    mid = with_transcript(env)
    base = f"/api/v1/meetings/{mid}/actions"
    created = env.client.post(base, json={"task": "Book room", "assignee": "Ann",
                                          "due_date": "2026-12-01T00:00:00", "priority": "high"})
    assert created.status_code == 201
    a = created.json(); aid = a["id"]
    assert a["status"] == "pending" and a["completed_at"] is None
    assert env.client.post(base, json={"task": "book ROOM!"}).status_code == 409
    assert env.client.post(base, json={"task": ""}).status_code == 422
    assert env.client.post(base, json={"task": "x", "priority": "urgent"}).status_code == 422

    done = env.client.patch(f"{base}/{aid}?status=completed").json()
    assert done["status"] == "completed"
    first = env.client.get(f"{base}/{aid}").json()["completed_at"]
    assert first
    env.client.patch(f"{base}/{aid}?status=completed")
    assert env.client.get(f"{base}/{aid}").json()["completed_at"] == first   # kept
    reopened = env.client.put(f"{base}/{aid}", json={"status": "in_progress"}).json()
    assert reopened["completed_at"] is None
    edited = env.client.put(f"{base}/{aid}", json={"assignee": "Bob", "priority": "low",
                                                   "task": "Book the big room"}).json()
    assert (edited["assignee"], edited["priority"], edited["task"]) == ("Bob", "low", "Book the big room")
    assert env.client.put(f"{base}/{aid}", json={"clear_due_date": True}).json()["due_date"] is None
    assert env.client.patch(f"{base}/{aid}?status=bogus").status_code == 400
    assert env.client.put(f"{base}/{aid}", json={"status": "bogus"}).status_code == 422

    assert env.client.delete(f"{base}/{aid}").json() == {"deleted": True, "action_id": aid}
    assert env.client.delete(f"{base}/{aid}").status_code == 404
    assert env.client.get(f"{base}/{aid}").status_code == 404
    assert env.client.get("/api/v1/meetings/ghost/actions").status_code == 404


# ------------------------------------------------------------- meeting basics

def test_start_and_end_meeting(env):
    r = env.client.post("/api/v1/meetings/start", json={"title": "Standup", "participants": ["a@x.com"]})
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    mid = r.json()["id"]
    ended = env.client.post(f"/api/v1/meetings/{mid}/end").json()
    assert ended["status"] == "completed" and ended["ended_at"]
    assert env.client.post("/api/v1/meetings/ghost/end").status_code == 404
    assert env.client.get(f"/api/v1/meetings/{mid}").json()["ended_at"]


def test_root_and_health(env):
    assert env.client.get("/").json()["name"] == "Meeting Intelligence Agent API"
    assert env.client.get("/health").json()["status"] == "healthy"


# --------------------------------------------------------------- calendar HTTP

def test_calendar_endpoint_filters_over_http(env):
    mid = with_transcript(env)
    env.client.post(f"/api/v1/meetings/{mid}/extract-actions")
    ev = lambda q="": env.client.get(f"/api/v1/calendar/tester@example.com/events{q}")
    allv = ev().json()
    assert allv["calendar_id"] == "internal" and allv["total"] == 2   # 1 meeting + 1 dated action ("soon" has none)
    types = {e["type"] for e in allv["events"]}
    assert types == {"meeting", "submission"}
    sub = ev("?event_type=submission&status=pending").json()
    assert sub["total"] == 1 and sub["events"][0]["summary"] == "Finish the report"
    assert sub["events"][0]["meeting_title"] == "Weekly sync"
    assert ev("?start_date=2030-01-01").json()["events"] == []
    assert ev("?end_date=2026-11-13&start_date=2026-11-13&event_type=submission").json()["total"] == 1
    assert ev("?max_results=1").json()["count"] == 1
    assert ev(f"?meeting_id={mid}").json()["total"] == 2
    for bad in ("?event_type=holiday", "?start_date=nope", "?max_results=0",
                "?start_date=2026-12-01&end_date=2026-11-01"):
        assert ev(bad).status_code == 422


def test_calendar_reflects_status_and_due_date_edits(env):
    mid = with_transcript(env)
    aid = env.client.post(f"/api/v1/meetings/{mid}/extract-actions").json()["action_items"][0]["id"]
    ev = lambda: {e["id"]: e for e in env.client.get(
        "/api/v1/calendar/tester@example.com/events?event_type=submission").json()["events"]}
    env.client.patch(f"/api/v1/meetings/{mid}/actions/{aid}?status=completed")
    assert ev()[aid]["status"] == "completed"
    env.client.put(f"/api/v1/meetings/{mid}/actions/{aid}", json={"due_date": "2027-03-01T00:00:00"})
    assert ev()[aid]["start"] == "2027-03-01T00:00:00"
    env.client.put(f"/api/v1/meetings/{mid}/actions/{aid}", json={"clear_due_date": True})
    assert aid not in ev()


# ------------------------------------------------------------------ readiness

class _Ollama(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass


def test_readiness_reports_dependencies(env, monkeypatch):
    sqlite = create_engine("sqlite://")
    import src.models.db as db_module
    monkeypatch.setattr(db_module, "engine", sqlite)

    server = http.server.HTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_port}")
    r = env.client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {
        "status": "ready", "checks": {"database": "ok", "redis": "ok", "ollama": "ok"}}
    server.shutdown()

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    r = env.client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "degraded"   # Ollama optional

    class DeadRedis:
        def ping(self): raise ConnectionError("down")
    from src.workers import jobs
    jobs.set_client(DeadRedis())
    r = env.client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["redis"] == "fail"
    assert "down" not in r.text                                        # no internals
    jobs.set_client(None)

    broken = create_engine("sqlite:////nonexistent_dir/x.db")
    monkeypatch.setattr(db_module, "engine", broken)
    assert env.client.get("/health/ready").status_code == 503


# ------------------------------------------------- audio / delete / task list

def test_audio_download_returns_the_stored_recording(env):
    mid = mk_meeting(env)
    r = env.client.get(f"/api/v1/meetings/{mid}/audio")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == open(TEST_WAV, "rb").read()
    assert "inline" in r.headers["content-disposition"] and "meeting.wav" in r.headers["content-disposition"]


def test_audio_404s(env):
    s = env.Session(); s.add(Meeting(id="noaudio", title="t")); s.commit(); s.close()
    assert env.client.get("/api/v1/meetings/noaudio/audio").status_code == 404
    assert env.client.get("/api/v1/meetings/ghost/audio").status_code == 404
    mid = mk_meeting(env)
    import os
    os.remove(env.Session().get(Meeting, mid).audio_file_path)
    assert env.client.get(f"/api/v1/meetings/{mid}/audio").status_code == 404


def test_delete_meeting_removes_everything_including_audio(env):
    from src.models.database import Summary
    import os
    mid = with_transcript(env)
    env.client.post(f"/api/v1/meetings/{mid}/summarize")
    env.client.post(f"/api/v1/meetings/{mid}/extract-actions")
    other = mk_meeting(env, title="Keep me")
    path = env.Session().get(Meeting, mid).audio_file_path
    assert os.path.exists(path)

    assert env.client.delete(f"/api/v1/meetings/{mid}").status_code == 204
    s = env.Session()
    assert s.get(Meeting, mid) is None
    for model in (Transcript, Summary, ActionItemDB):
        assert s.query(model).filter_by(meeting_id=mid).count() == 0
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))
    assert s.get(Meeting, other) is not None                       # others untouched
    assert env.client.get(f"/api/v1/meetings/{mid}").status_code == 404
    assert env.client.delete(f"/api/v1/meetings/{mid}").status_code == 404


def test_delete_is_refused_while_processing(env):
    from src.workers import jobs
    mid = mk_meeting(env)
    jobs.create_job(mid, ["transcribe"], {}, job_id="j1")
    jobs.acquire_lock(mid, "j1")
    r = env.client.delete(f"/api/v1/meetings/{mid}")
    assert r.status_code == 409 and "processed" in r.json()["detail"]
    assert env.client.get(f"/api/v1/meetings/{mid}").status_code == 200
    j = jobs.get_job("j1"); j["state"] = "succeeded"; jobs.save_job(j)
    assert env.client.delete(f"/api/v1/meetings/{mid}").status_code == 204


def test_meeting_payload_has_owner_permission_and_stats(env):
    mid = with_transcript(env)
    env.client.post(f"/api/v1/meetings/{mid}/summarize")
    env.client.post(f"/api/v1/meetings/{mid}/extract-actions")
    env.client.patch(f"/api/v1/meetings/{mid}/actions/"
                     f"{env.client.get(f'/api/v1/meetings/{mid}/actions').json()['action_items'][0]['id']}?status=completed")
    for payload in (env.client.get(f"/api/v1/meetings/{mid}").json(),
                    env.client.get("/api/v1/meetings").json()["meetings"][0]):
        assert payload["owner_email"] == "tester@example.com" and payload["can_write"] is True
        assert (payload["has_transcript"], payload["has_summary"]) == (True, True)
        assert (payload["action_count"], payload["open_action_count"]) == (2, 1)
    bare = upload(env.client, title="Bare").json()["meeting_id"]
    p = env.client.get(f"/api/v1/meetings/{bare}").json()
    assert (p["has_transcript"], p["action_count"]) == (False, 0)


def seed_actions(env):
    s = env.Session()
    s.add(Meeting(id="m1", title="Alpha", owner_email="tester@example.com"))
    s.add(Meeting(id="m2", title="Beta", owner_email="tester@example.com"))
    d = lambda day: datetime(2026, 11, day)
    s.add_all([
        ActionItemDB(id="a1", meeting_id="m1", task="Write report", assignee="Speaker 1", due_date=d(20), status="pending"),
        ActionItemDB(id="a2", meeting_id="m1", task="Book room", assignee="Ann", due_date=d(5), status="completed"),
        ActionItemDB(id="a3", meeting_id="m2", task="Review budget", assignee="Speaker 1", due_date=None, status="in_progress"),
        ActionItemDB(id="a4", meeting_id="m2", task="Send invites", assignee="Bob", due_date=d(10), status="pending"),
    ])
    s.commit(); s.close()


def test_actions_list_across_meetings_with_filters(env):
    seed_actions(env)
    get = lambda q="": env.client.get(f"/api/v1/actions{q}").json()
    allv = get()
    assert allv["total"] == 4 and [a["id"] for a in allv["action_items"]] == ["a2", "a4", "a1", "a3"]  # due asc, undated last
    first = allv["action_items"][0]
    assert first["meeting_title"] == "Alpha" and first["can_write"] is True and first["meeting_id"] == "m1"
    assert {a["id"] for a in get("?status=pending,in_progress")["action_items"]} == {"a1", "a3", "a4"}
    assert [a["id"] for a in get("?assignee=speaker")["action_items"]] == ["a1", "a3"]
    assert [a["id"] for a in get("?meeting_id=m2")["action_items"]] == ["a4", "a3"]
    assert [a["id"] for a in get("?due_from=2026-11-06&due_to=2026-11-20")["action_items"]] == ["a4", "a1"]
    assert [a["id"] for a in get("?has_due_date=false")["action_items"]] == ["a3"]
    assert get("?has_due_date=true")["total"] == 3
    assert get("?limit=2&offset=1")["count"] == 2 and get("?limit=2")["total"] == 4
    assert get("?sort=created")["total"] == 4
    assert get("?meeting_id=ghost") == {"count": 0, "total": 0, "action_items": []}


def test_actions_list_validation(env):
    for bad in ("?status=bogus", "?due_from=nope", "?sort=random", "?limit=0", "?limit=501"):
        assert env.client.get(f"/api/v1/actions{bad}").status_code == 422


# ------------------------------------------------- online meetings captured from a browser tab

def test_an_online_meeting_recording_needs_consent_and_remembers_it(env):
    r = upload(env.client, source="online_tab")
    assert r.status_code == 422 and "everyone in the meeting knows" in r.json()["detail"]
    assert env.Session().query(Meeting).count() == 0                               # nothing was stored
    r = upload(env.client, source="online_tab", recording_consent="false")
    assert r.status_code == 422
    r = upload(env.client, source="online_tab", recording_consent="true")
    assert r.status_code == 201
    m = env.Session().get(Meeting, r.json()["meeting_id"])
    assert m.source == "online_tab" and m.consent_at is not None
    assert abs((datetime.utcnow() - m.consent_at).total_seconds()) < 60
    d = env.client.get(f"/api/v1/meetings/{m.id}").json()
    assert d["source"] == "online_tab" and d["consent_at"] == m.consent_at.isoformat()


def test_other_sources_do_not_need_consent_and_unknown_ones_are_refused(env):
    for src in ("upload", "browser_mic"):
        r = upload(env.client, source=src)
        assert r.status_code == 201
        m = env.Session().get(Meeting, r.json()["meeting_id"])
        assert m.source == src and m.consent_at is None
    assert upload(env.client, source="telepathy").status_code == 422
    plain = upload(env.client)
    m = env.Session().get(Meeting, plain.json()["meeting_id"])
    assert m.source is None and m.consent_at is None                               # older behaviour unchanged
    assert env.client.get(f"/api/v1/meetings/{m.id}").json()["source"] is None


def test_consent_given_for_a_file_upload_is_not_recorded_as_online_consent(env):
    r = upload(env.client, source="upload", recording_consent="true")
    assert env.Session().get(Meeting, r.json()["meeting_id"]).consent_at is None


import shutil as _shutil
import subprocess as _subprocess


@pytest.mark.skipif(_shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_browser_recorded_webm_of_an_online_meeting_is_accepted_with_its_real_length(env, tmp_path):
    """MediaRecorder WebM has no length in its header; the upload must still measure it."""
    webm = tmp_path / "online.webm"
    with open(webm, "wb") as f:
        _subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", TEST_WAV, "-c:a", "libopus", "-b:a", "64k", "-f", "webm", "-"],
                        stdout=f, check=True)
    r = upload(env.client, name="Online meeting 16 Nov. 11.22 PM.webm", data=webm.read_bytes(),
               source="online_tab", recording_consent="true", title="Weekly sync")
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["audio_duration_seconds"] == pytest.approx(6.3, abs=0.4)             # measured by decoding, not 0
    m = env.Session().get(Meeting, d["meeting_id"])
    assert m.source == "online_tab" and m.consent_at is not None and m.audio_original_filename.endswith(".webm")
    assert m.duration_minutes == 1
    assert env.client.get(f"/api/v1/meetings/{m.id}").json()["audio_seconds"] == pytest.approx(6.3, abs=0.4)
