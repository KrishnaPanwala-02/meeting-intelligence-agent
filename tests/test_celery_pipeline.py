"""Background processing: Celery tasks, job tracking, locking, API."""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agent.action_extractor import ActionExtractionError
from src.agent.summarizer import SummarizationError
from src.api.main import app
from src.models.database import (ActionItemDB, Base, Meeting, Summary,
                                 Transcript)
from src.models.db import get_db
from src.services.auth import get_current_user
from tests.conftest import admin_override
from src.models.schemas import (ActionItem, KeyPoint, MeetingSummary,
                                TranscriptionResult, TranscriptSegment)
from src.services import pipeline
from src.workers import jobs, tasks


class FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def expire(self, key, seconds):
        return True

    def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio_path, language="en", identify_speakers=False, known_voices=None):
        self.calls += 1
        segs = [
            TranscriptSegment(id=0, start_time=0, end_time=3, speaker="Speaker 1",
                              text="I will finish the report by Friday."),
            TranscriptSegment(id=1, start_time=3, end_time=6, speaker="Speaker 2",
                              text="I will book the room by Monday."),
        ]
        return TranscriptionResult(
            audio_file=audio_path, language=language, segments=segs,
            duration_seconds=6.0, processing_seconds=0.1, word_count=14,
            speaker_count=2 if identify_speakers else 0,
            created_at=datetime.now())


class FakeSummarizer:
    def __init__(self, failures=0):
        self.failures, self.calls = failures, 0

    async def summarize(self, transcript, meeting_title="", participants=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise SummarizationError("The Ollama service is unavailable")
        return MeetingSummary(
            meeting_title=meeting_title, executive_summary="Two commitments.",
            key_points=[KeyPoint(topic="Report", description="Due Friday")],
            decisions=[], next_steps=["Finish report"], key_quotes=[],
            participants=[], duration_minutes=1, word_count=14,
            created_at=datetime.now())


class FakeExtractor:
    def __init__(self, failures=0, crash=False):
        self.failures, self.crash, self.calls = failures, crash, 0

    async def extract(self, transcript, use_ai=True, reference_date=None):
        self.calls += 1
        if self.crash:
            raise RuntimeError("secret internal detail postgres://u:p@h/db")
        if self.calls <= self.failures:
            raise ActionExtractionError("The Ollama service is unavailable")
        return [
            ActionItem(task="Finish the report", assignee="Speaker 1",
                       due_date=datetime(2026, 11, 13), priority="high",
                       due_date_phrase="by Friday"),
            ActionItem(task="Book the room", assignee="Speaker 2",
                       due_date=None, due_date_phrase="soon"),
        ]


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    fake_redis = FakeRedis()
    jobs.set_client(fake_redis)
    monkeypatch.setattr(tasks, "session_factory", Session)
    monkeypatch.setattr(tasks, "MAX_RETRIES", 2)

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    s = Session()
    s.add(Meeting(id="m1", title="Sync", status="uploaded",
                  participants=["a@x.com"], audio_file_path=str(audio),
                  created_at=datetime(2026, 11, 11, 9, 0)))
    s.add(Meeting(id="m2", title="No audio", status="uploaded"))
    s.commit()
    s.close()

    e = type("Env", (), {})()
    e.Session, e.redis = Session, fake_redis
    e.transcriber, e.summarizer, e.extractor = (
        FakeTranscriber(), FakeSummarizer(), FakeExtractor())
    monkeypatch.setattr(pipeline, "get_transcriber", lambda: e.transcriber)
    monkeypatch.setattr(pipeline, "make_summarizer", lambda: e.summarizer)
    monkeypatch.setattr(pipeline, "make_extractor", lambda: e.extractor)
    yield e
    jobs.set_client(None)


def run(env, meeting_id="m1", steps=None, **opts):
    steps = steps or list(pipeline.STEPS)
    job_id = str(uuid.uuid4())
    jobs.create_job(meeting_id, steps, opts, job_id=job_id)
    tasks.process_meeting.apply(args=[job_id, meeting_id, opts])
    return jobs.get_job(job_id)


def counts(env):
    s = env.Session()
    out = (s.query(Transcript).count(), s.query(Summary).count(),
           s.query(ActionItemDB).count())
    s.close()
    return out


def test_full_pipeline_success_saves_everything(env):
    job = run(env, identify_speakers=True)
    assert job["state"] == "succeeded" and job["progress_percent"] == 100
    assert [s["state"] for s in job["steps"]] == ["succeeded"] * 3
    assert job["steps"][0]["result"]["speaker_count"] == 2
    assert counts(env) == (1, 1, 2)
    s = env.Session()
    t = s.query(Transcript).one()
    assert t.meeting_id == "m1" and t.segments[0]["speaker"] == "Speaker 1"
    a = {x.task: x for x in s.query(ActionItemDB).all()}
    assert a["Finish the report"].due_date == datetime(2026, 11, 13)
    assert a["Book the room"].due_date is None
    assert s.query(Summary).one().meeting_id == "m1"
    s.close()
    assert env.redis.get("job:lock:m1") is None          # lock released
    assert jobs.latest_job_id("m1") == job["job_id"]


def test_rerun_creates_no_duplicates(env):
    run(env)
    job2 = run(env, steps=["summarize", "extract_actions"])
    assert job2["state"] == "succeeded"
    assert job2["steps"][0]["result"]["already_existed"] is True
    assert job2["steps"][1]["result"]["skipped_duplicates"] == 2
    assert counts(env) == (1, 1, 2)


def test_single_step_tasks(env):
    job_id = str(uuid.uuid4())
    jobs.create_job("m1", ["transcribe"], {}, job_id=job_id)
    tasks.transcribe_audio.apply(args=[job_id, "m1", {"identify_speakers": False}])
    assert jobs.get_job(job_id)["state"] == "succeeded"
    assert counts(env) == (1, 0, 0)


def test_missing_audio_fails_permanently_without_retry(env):
    job = run(env, meeting_id="m2")
    assert job["state"] == "failed" and "audio" in job["error"].lower()
    assert job["attempts"] == 1
    assert [s["state"] for s in job["steps"]] == [
        "failed", "skipped_due_to_failure", "skipped_due_to_failure"]
    assert counts(env) == (0, 0, 0)
    assert env.redis.get("job:lock:m2") is None


def test_unknown_meeting_fails(env):
    job = run(env, meeting_id="ghost")
    assert job["state"] == "failed" and job["error"] == "Meeting not found"


def test_transient_failure_retries_and_skips_finished_steps(env):
    env.extractor.failures = 1
    job = run(env)
    assert job["state"] == "succeeded"
    assert job["attempts"] == 2
    assert env.transcriber.calls == 1          # not repeated on retry
    assert env.summarizer.calls == 1           # finished step skipped
    assert env.extractor.calls == 2
    assert counts(env) == (1, 1, 2)


def test_transient_failure_exhausts_retries(env):
    env.summarizer.failures = 99
    job = run(env)
    assert job["state"] == "failed"
    assert job["attempts"] == 3                # first try + 2 retries
    assert "Ollama" in job["error"]
    assert [s["state"] for s in job["steps"]] == [
        "succeeded", "failed", "skipped_due_to_failure"]
    assert counts(env) == (1, 0, 0)            # earlier work preserved
    assert env.redis.get("job:lock:m1") is None


def test_unexpected_error_is_sanitized(env):
    env.extractor.crash = True
    job = run(env)
    assert job["state"] == "failed"
    assert job["error"] == "Internal error while processing"
    assert "postgres://" not in str(job)


def test_duplicate_execution_prevented_by_lock(env):
    assert jobs.acquire_lock("m1", "other-job") is None
    job = run(env)
    assert job["state"] == "failed" and "other-job" in job["error"]
    assert counts(env) == (0, 0, 0)
    assert env.redis.get("job:lock:m1") == "other-job"   # not stolen


def test_stale_lock_reclaimed_only_when_job_finished(env):
    jobs.create_job("m1", ["transcribe"], {}, job_id="old")
    jobs.acquire_lock("m1", "old")
    assert jobs.reclaim_if_stale("m1") is False          # still queued
    j = jobs.get_job("old"); j["state"] = "failed"; jobs.save_job(j)
    assert jobs.reclaim_if_stale("m1") is True
    jobs.acquire_lock("m1", "ghost")                      # job record missing
    assert jobs.reclaim_if_stale("m1") is True


# ------------------------------------------------------------------- API

@pytest.fixture
def client(env, monkeypatch):
    def override():
        d = env.Session()
        try:
            yield d
        finally:
            d.close()

    sent = []

    class FakeTask:
        def apply_async(self, args, task_id):
            sent.append((args, task_id))

    monkeypatch.setattr(tasks, "task_for", lambda steps: FakeTask())
    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_current_user] = admin_override
    c = TestClient(app)
    c.sent = sent
    yield c
    app.dependency_overrides.clear()


def test_process_endpoint_queues_job(client, env):
    r = client.post("/api/v1/meetings/m1/process")
    assert r.status_code == 202
    body = r.json()
    assert body["steps"] == list(pipeline.STEPS) and body["state"] == "queued"
    assert client.sent[0][1] == body["job_id"]            # celery id == job id
    status = client.get(body["status_url"]).json()
    assert status["state"] == "queued" and status["progress_percent"] == 0
    latest = client.get("/api/v1/meetings/m1/jobs/latest").json()
    assert latest["job_id"] == body["job_id"]


def test_process_endpoint_rejects_duplicates(client, env):
    first = client.post("/api/v1/meetings/m1/process").json()
    r = client.post("/api/v1/meetings/m1/process")
    assert r.status_code == 409
    assert r.json()["detail"]["job_id"] == first["job_id"]
    assert len(client.sent) == 1


def test_process_endpoint_reclaims_finished_lock(client, env):
    first = client.post("/api/v1/meetings/m1/process").json()
    j = jobs.get_job(first["job_id"]); j["state"] = "failed"; jobs.save_job(j)
    assert client.post("/api/v1/meetings/m1/process").status_code == 202


def test_process_endpoint_validation(client, env):
    assert client.post("/api/v1/meetings/ghost/process").status_code == 404
    assert client.post("/api/v1/meetings/m2/process").status_code == 400
    assert client.post("/api/v1/meetings/m1/process",
                       json={"steps": ["transcribe", "explode"]}).status_code == 422
    assert client.post("/api/v1/meetings/m1/process",
                       json={"steps": []}).status_code == 422
    assert client.post("/api/v1/meetings/m1/process",
                       json={"language": "english!"}).status_code == 422
    # summarize-only needs an existing transcript
    r = client.post("/api/v1/meetings/m1/process", json={"steps": ["summarize"]})
    assert r.status_code == 400 and "transcript" in r.json()["detail"].lower()


def test_steps_are_ordered_canonically(client, env):
    r = client.post("/api/v1/meetings/m1/process",
                    json={"steps": ["extract_actions", "transcribe"]})
    assert r.json()["steps"] == ["transcribe", "extract_actions"]


def test_broker_down_returns_503_and_frees_lock(client, env, monkeypatch):
    class Broken:
        def apply_async(self, args, task_id):
            raise ConnectionError("redis down")

    monkeypatch.setattr(tasks, "task_for", lambda steps: Broken())
    r = client.post("/api/v1/meetings/m1/process")
    assert r.status_code == 503
    assert env.redis.get("job:lock:m1") is None
    monkeypatch.undo()


def test_job_status_not_found(client, env):
    assert client.get("/api/v1/jobs/nope").status_code == 404
    assert client.get("/api/v1/meetings/m1/jobs/latest").status_code == 404
    assert client.get("/api/v1/meetings/ghost/jobs/latest").status_code == 404


def test_registered_tasks_are_real():
    names = set(tasks.celery_app.tasks)
    assert {"process_meeting", "transcribe_audio", "generate_summary",
            "extract_action_items"} <= names
