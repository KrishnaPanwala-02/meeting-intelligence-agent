"""
Integration tests against a real PostgreSQL server.

They run in a scratch database (never the application's), which is created
before and dropped after the session. Skipped if PostgreSQL is unreachable.
Covers what SQLite cannot: the schema migration, JSON/FK behaviour and
concurrency guarantees (advisory locks, row locks, unique constraints).
"""

import os
import threading
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.api import main
from src.models.database import (ActionItemDB, Base, Meeting, Summary,
                                 Transcript, User)
from src.models.db import get_db, init_db
from src.services import auth
from src.services.auth import get_current_user
from tests.conftest import admin_override
from tests.test_celery_pipeline import FakeExtractor, FakeTranscriber

pytestmark = pytest.mark.integration

TEST_DB = "meeting_intelligence_test"


def _urls():
    real = os.environ.get("DATABASE_URL", "")
    if not real.startswith("postgresql"):
        pytest.skip("DATABASE_URL is not a PostgreSQL URL")
    u = urlparse(real)
    real_name = u.path.lstrip("/")
    assert real_name != TEST_DB
    admin = real.replace(f"/{real_name}", "/postgres")
    return admin, real.replace(f"/{real_name}", f"/{TEST_DB}"), real_name


@pytest.fixture(scope="module")
def pg_engine():
    admin_url, test_url, real_name = _urls()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
            c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as exc:
        pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
    engine = create_engine(test_url, pool_size=20, max_overflow=20)
    # Safety: the engine must point at the scratch database.
    assert engine.url.database == TEST_DB != real_name
    yield engine
    engine.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}"'))
    admin.dispose()


@pytest.fixture
def pg(pg_engine):
    """Clean schema per test. Every session handed out is closed on teardown
    (an open transaction would block the next test's DROP TABLE)."""
    opened = []
    Base.metadata.drop_all(pg_engine)
    init_db(pg_engine)
    maker = sessionmaker(bind=pg_engine)

    def factory():
        session = maker()
        opened.append(session)
        return session

    yield factory
    for session in opened:
        session.close()


# --------------------------------------------------------------- migration

def test_migration_upgrades_an_old_schema_without_losing_data(pg_engine):
    with pg_engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
        # the schema as first released (no audio / owner columns, no auth tables)
        c.execute(text("""CREATE TABLE meetings (
            id VARCHAR PRIMARY KEY, title VARCHAR NOT NULL, status VARCHAR,
            scheduled_time TIMESTAMP, started_at TIMESTAMP, ended_at TIMESTAMP,
            duration_minutes INTEGER, participants JSON, created_at TIMESTAMP)"""))
        c.execute(text("""INSERT INTO meetings (id, title, status, participants)
                          VALUES ('legacy-1', 'Old meeting', 'completed', '["a@x.com"]')"""))
    init_db(pg_engine)
    init_db(pg_engine)                                    # idempotent
    with pg_engine.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='meetings'"))}
        assert {"audio_file_path", "audio_original_filename", "audio_size_bytes",
                "owner_email"} <= cols
        row = c.execute(text("SELECT title, status, owner_email, participants FROM meetings")).one()
        assert row[0] == "Old meeting" and row[2] is None and row[3] == ["a@x.com"]
        tables = {r[0] for r in c.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
        assert {"users", "auth_sessions", "refresh_tokens", "password_reset_tokens",
                "transcripts", "summaries", "action_items"} <= tables
        idx = {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename='meetings'"))}
        assert "ix_meetings_owner_email" in idx


# ------------------------------------------------------------- persistence

def test_json_columns_round_trip_unicode_and_nesting(pg):
    s = pg()
    segs = [{"id": 0, "start_time": 0.5, "end_time": 2.25, "text": "Grüße — 你好 \"quoted\"",
             "speaker": "Speaker 1", "confidence": 0.79}]
    s.add(Meeting(id="m", title="t", participants=["a@x.com", "ü@x.com"]))
    s.add(Transcript(id="t", meeting_id="m", language="en", full_text="x", segments=segs))
    s.add(Summary(id="s", meeting_id="m", executive_summary="e",
                  key_points=[{"topic": "T", "description": "D"}], decisions=[], next_steps=["a"], key_quotes=[]))
    s.commit(); s.close()
    s = pg()
    assert s.get(Transcript, "t").segments == segs
    assert s.get(Meeting, "m").participants == ["a@x.com", "ü@x.com"]
    assert s.get(Summary, "s").key_points[0]["topic"] == "T"


def test_foreign_keys_are_enforced(pg):
    s = pg()
    s.add(Transcript(id="t", meeting_id="does-not-exist", segments=[]))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.add(ActionItemDB(id="a", meeting_id="nope", task="x"))
    with pytest.raises(IntegrityError):
        s.commit()


def test_unique_constraints(pg):
    s = pg()
    s.add(User(id="1", email="a@x.com", password_hash="h")); s.commit()
    s.add(User(id="2", email="a@x.com", password_hash="h"))
    with pytest.raises(IntegrityError):
        s.commit()


def test_timestamps_and_status_lifecycle_persist(pg):
    s = pg()
    s.add(Meeting(id="m", title="t"))
    due = datetime(2026, 11, 13, 0, 0)
    s.add(ActionItemDB(id="a", meeting_id="m", task="x", due_date=due))
    s.commit()
    a = pg().get(ActionItemDB, "a")
    assert (a.due_date, a.status, a.priority, a.completed_at) == (due, "pending", "medium", None)
    assert a.created_at is not None


# ------------------------------------------------------------- concurrency

@pytest.fixture
def api(pg, monkeypatch):
    monkeypatch.setattr(main, "action_extractor", FakeExtractor())
    monkeypatch.setattr(main, "transcriber", FakeTranscriber())

    def override():
        d = pg()
        try:
            yield d
        finally:
            d.close()
    main.app.dependency_overrides[get_db] = override
    main.app.dependency_overrides[get_current_user] = admin_override
    yield pg
    main.app.dependency_overrides.clear()


def run_parallel(n, fn):
    results, barrier = [], threading.Barrier(n)

    def worker(i):
        client = TestClient(main.app)
        barrier.wait()
        results.append(fn(client, i))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]; [t.join() for t in threads]
    return results


def future(days=3, hour=10):
    return (datetime.now() + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_simultaneous_bookings_of_one_slot_yield_exactly_one_meeting(api, monkeypatch):
    # Widen the check-then-insert window so an unprotected implementation
    # reliably double-books (verified by disabling the advisory lock).
    import time
    from src.services import scheduling
    original = scheduling.find_conflicts

    def slow_find_conflicts(*a, **k):
        result = original(*a, **k)
        time.sleep(0.2)
        return result
    monkeypatch.setattr(scheduling, "find_conflicts", slow_find_conflicts)
    when = future()
    codes = run_parallel(8, lambda c, i: c.post("/api/v1/meetings/schedule", json={
        "title": f"M{i}", "participants": ["a@x.com"], "scheduled_time": when,
        "duration_minutes": 60}).status_code)
    assert Counter(codes) == Counter({201: 1, 409: 7})
    assert api().query(Meeting).count() == 1


def test_simultaneous_bookings_of_distinct_slots_all_succeed(api):
    codes = run_parallel(6, lambda c, i: c.post("/api/v1/meetings/schedule", json={
        "title": f"M{i}", "participants": ["a@x.com"],
        "scheduled_time": future(3 + i), "duration_minutes": 60}).status_code)
    assert codes == [201] * 6


def test_simultaneous_extractions_never_duplicate_action_items(api):
    s = api()
    s.add(Meeting(id="m", title="t", owner_email="tester@example.com"))
    s.add(Transcript(id="t", meeting_id="m", language="en", full_text="x", segments=[
        {"id": 0, "start_time": 0, "end_time": 1, "text": "hi", "speaker": "Speaker 1", "confidence": 1}]))
    s.commit(); s.close()
    codes = run_parallel(8, lambda c, i: c.post("/api/v1/meetings/m/extract-actions").status_code)
    assert codes == [200] * 8
    tasks = [r.task for r in api().query(ActionItemDB).filter_by(meeting_id="m")]
    assert sorted(tasks) == ["Book the room", "Finish the report"]      # 2 rows, not 16


def test_simultaneous_manual_creates_of_the_same_task_yield_one(api):
    s = api(); s.add(Meeting(id="m", title="t")); s.commit(); s.close()
    codes = run_parallel(6, lambda c, i: c.post("/api/v1/meetings/m/actions",
                                                json={"task": "Book room"}).status_code)
    assert Counter(codes) == Counter({201: 1, 409: 5})


def test_simultaneous_registration_of_one_email_yields_one_account(api):
    codes = run_parallel(8, lambda c, i: c.post("/auth/register", json={
        "email": "race@example.com", "password": "correct horse battery"}).status_code)
    assert sorted(codes) == [201] + [409] * 7
    assert api().query(User).count() == 1


def test_refresh_token_can_be_redeemed_only_once_under_a_race(api):
    c0 = TestClient(main.app)
    c0.post("/auth/register", json={"email": "r@example.com", "password": "correct horse battery"})
    tokens = c0.post("/auth/login", json={"email": "r@example.com",
                                          "password": "correct horse battery"}).json()
    codes = run_parallel(8, lambda c, i: c.post("/auth/refresh", json={
        "refresh_token": tokens["refresh_token"]}).status_code)
    assert codes.count(200) == 1 and codes.count(401) == 7
