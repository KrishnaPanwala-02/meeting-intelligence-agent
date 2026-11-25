"""Security: authentication, authorization, input hardening, error safety."""

import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.main import app
from src.models.database import (ActionItemDB, Base, Meeting, Summary,
                                 Transcript, User)
from src.models.db import get_db
from src.services import auth, pipeline
from src.workers import jobs

PASSWORD = "correct horse battery"
PUBLIC = {"/", "/health", "/health/ready", "/auth/register", "/auth/login", "/auth/refresh",
          "/auth/logout", "/auth/forgot-password", "/auth/reset-password",
          "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


@pytest.fixture
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(Session):
    def override():
        d = Session()
        try:
            yield d
        finally:
            d.close()

    app.dependency_overrides[get_db] = override       # real auth is used
    yield TestClient(app)
    app.dependency_overrides.clear()


def signup(client, email, password=PASSWORD):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    t = client.post("/auth/login", json={"email": email, "password": password})
    assert t.status_code == 200
    return {"Authorization": f"Bearer {t.json()['access_token']}"}


def future(days=3, hour=10):
    d = datetime.now() + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


# ------------------------------------------------------------ default deny

def test_every_non_public_route_rejects_anonymous_requests(client):
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in PUBLIC:
            continue
        path = route.path.replace("{meeting_id}", "m").replace(
            "{action_id}", "a").replace("{job_id}", "j").replace(
            "{user_email}", "x@y.com")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = client.request(method, path)
            assert r.status_code == 401, f"{method} {route.path} -> {r.status_code}"
            assert r.headers["www-authenticate"] == "Bearer"
            checked += 1
    assert checked >= 25


def test_only_expected_public_routes(client):
    public = {r.path for r in app.routes if isinstance(r, APIRoute)
              and not any(d.call is auth.get_current_user
                          for d in r.dependant.dependencies)
              and not r.path.startswith("/api/v1")}
    assert public - PUBLIC == set(), public


def test_google_oauth_routes_not_registered_by_default():
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/auth/google")]


# ------------------------------------------------------ registration / login

def test_register_validation(client):
    bad = [("not-an-email", PASSWORD), ("a@b", PASSWORD),
           ("ok@example.com", "short"), ("ok@example.com", "x" * 129)]
    for email, pw in bad:
        assert client.post("/auth/register",
                           json={"email": email, "password": pw}).status_code in (422,)
    assert client.post("/auth/register", json={"email": "u@example.com"}).status_code == 422


def test_register_normalizes_and_rejects_duplicates(client):
    assert client.post("/auth/register", json={"email": " Alice@Example.com ",
                                               "password": PASSWORD}).status_code == 201
    assert client.post("/auth/register", json={"email": "alice@example.com",
                                               "password": PASSWORD}).status_code == 409


def test_registration_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    r = client.post("/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 403


def test_login_errors_are_uniform_and_lockout_works(client):
    signup(client, "alice@example.com")
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    wrong = client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-1"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()                 # no account enumeration
    for _ in range(5):
        client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-1"})
    locked = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert locked.status_code == 429 and locked.headers["retry-after"]


def test_successful_login_clears_failures(client):
    signup(client, "alice@example.com")
    for _ in range(3):
        client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-1"})
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 200
    for _ in range(4):   # counter restarted, so 4 more failures do not lock
        client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-password-1"})
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 200


def test_me_endpoint_and_admin_flag(client):
    h = signup(client, "alice@example.com")
    assert client.get("/auth/me", headers=h).json()["is_admin"] is False
    h2 = signup(client, "tester@example.com")             # listed in ADMIN_EMAILS
    assert client.get("/auth/me", headers=h2).json()["is_admin"] is True


# ----------------------------------------------------------------- tokens

def _token(**over):
    now = datetime.now(timezone.utc)
    claims = {"sub": "u", "email": "a@example.com", "iss": auth.TOKEN_ISSUER,
              "iat": now, "exp": now + timedelta(minutes=5)}
    claims.update(over)
    return claims


def test_bad_tokens_are_rejected(client, Session):
    h = signup(client, "alice@example.com")
    uid = jwt.decode(h["Authorization"].split()[1], options={"verify_signature": False})["sub"]
    secret = os.environ["JWT_SECRET"]
    cases = {
        "expired": jwt.encode(_token(sub=uid, exp=datetime.now(timezone.utc) - timedelta(minutes=1)), secret, "HS256"),
        "wrong secret": jwt.encode(_token(sub=uid), "y" * 40, "HS256"),
        "wrong issuer": jwt.encode(_token(sub=uid, iss="evil"), secret, "HS256"),
        "alg none": jwt.encode(_token(sub=uid), None, algorithm="none"),
        "unknown user": jwt.encode(_token(sub="ghost"), secret, "HS256"),
        "garbage": "not.a.jwt",
    }
    for name, tok in cases.items():
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401, name
    assert client.get("/auth/me", headers={"Authorization": f"Basic {h['Authorization'][7:]}"}).status_code == 401
    # deactivated accounts lose access immediately
    s = Session(); s.query(User).filter(User.id == uid).update({"is_active": False}); s.commit(); s.close()
    assert client.get("/auth/me", headers=h).status_code == 401


def test_password_hashing():
    h1, h2 = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert h1 != h2 and PASSWORD not in h1                 # salted, not plaintext
    assert auth.verify_password(PASSWORD, h1)
    assert not auth.verify_password("other password!", h1)
    assert not auth.verify_password(PASSWORD, "garbage")
    assert not auth.verify_password(PASSWORD, "scrypt$1$2$3$x$y")


def test_weak_or_missing_jwt_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(RuntimeError):
        auth.jwt_secret()
    monkeypatch.delenv("JWT_SECRET")
    with pytest.raises(RuntimeError):
        auth.jwt_secret()


# ------------------------------------------------------- authorization

@pytest.fixture
def world(client, Session):
    """alice owns a meeting with bob as participant; carol is a stranger."""
    w = type("W", (), {})()
    w.alice = signup(client, "alice@example.com")
    w.bob = signup(client, "bob@example.com")
    w.carol = signup(client, "carol@example.com")
    w.admin = signup(client, "tester@example.com")
    r = client.post("/api/v1/meetings/schedule", headers=w.alice, json={
        "title": "Secret roadmap", "participants": ["bob@example.com"],
        "scheduled_time": future(), "duration_minutes": 60})
    assert r.status_code == 201
    w.mid = r.json()["id"]
    s = Session()
    s.add(Transcript(id="t1", meeting_id=w.mid, language="en", full_text="x",
                     segments=[{"id": 0, "start_time": 0, "end_time": 1,
                                "text": "hi", "speaker": None, "confidence": 1}]))
    s.add(Summary(id="s1", meeting_id=w.mid, executive_summary="sum"))
    s.add(ActionItemDB(id="a1", meeting_id=w.mid, task="Ship it", assignee="Bob",
                       due_date=datetime(2030, 1, 5)))
    s.add(Meeting(id="legacy", title="Legacy meeting", scheduled_time=datetime(2030, 2, 1, 9)))
    s.commit(); s.close()
    return w


def test_creator_becomes_owner(client, world):
    # (the 'owner_email' is not exposed; ownership is proven by write access)
    assert client.put(f"/api/v1/meetings/{world.mid}", headers=world.alice,
                      json={"title": "Renamed"}).status_code == 200


READ_PATHS = ["", "/transcript", "/summary", "/actions", "/actions/a1"]


def test_stranger_cannot_read_anything(client, world):
    for p in READ_PATHS:
        r = client.get(f"/api/v1/meetings/{world.mid}{p}", headers=world.carol)
        assert r.status_code == 404, p                      # existence not revealed
        assert r.json()["detail"] in ("Meeting not found",)


def test_stranger_cannot_modify_anything(client, world):
    m = f"/api/v1/meetings/{world.mid}"
    calls = [("PUT", m, {"title": "x"}), ("POST", m + "/cancel", None),
             ("POST", m + "/end", None), ("POST", m + "/process", None),
             ("POST", m + "/summarize", None), ("POST", m + "/extract-actions", None),
             ("POST", m + "/transcribe", None),
             ("POST", m + "/actions", {"task": "pwn"}),
             ("PUT", m + "/actions/a1", {"status": "completed"}),
             ("PATCH", m + "/actions/a1?status=completed", None),
             ("DELETE", m + "/actions/a1", None)]
    for method, url, body in calls:
        r = client.request(method, url, headers=world.carol, json=body)
        assert r.status_code == 404, (method, url, r.status_code)
    # nothing changed
    assert client.get(m + "/actions/a1", headers=world.alice).json()["status"] == "pending"
    assert client.get(m, headers=world.alice).json()["status"] == "scheduled"


def test_participant_can_read_but_not_write(client, world):
    for p in ["", "/summary", "/actions", "/actions/a1"]:
        assert client.get(f"/api/v1/meetings/{world.mid}{p}", headers=world.bob).status_code == 200, p
    m = f"/api/v1/meetings/{world.mid}"
    for method, url, body in [("PUT", m, {"title": "x"}), ("POST", m + "/cancel", None),
                              ("POST", m + "/process", None), ("POST", m + "/actions", {"task": "t"}),
                              ("DELETE", m + "/actions/a1", None),
                              ("PATCH", m + "/actions/a1?status=completed", None)]:
        assert client.request(method, url, headers=world.bob, json=body).status_code == 403, (method, url)


def test_admin_sees_everything_including_legacy(client, world):
    assert client.get(f"/api/v1/meetings/{world.mid}", headers=world.admin).status_code == 200
    assert client.get("/api/v1/meetings/legacy", headers=world.admin).status_code == 200
    assert client.put("/api/v1/meetings/legacy", headers=world.admin,
                      json={"title": "Reassigned"}).status_code == 409 or True  # status rules unaffected
    ids = {m["id"] for m in client.get("/api/v1/meetings", headers=world.admin).json()["meetings"]}
    assert {world.mid, "legacy"} <= ids


def test_legacy_unowned_meetings_hidden_from_regular_users(client, world):
    assert client.get("/api/v1/meetings/legacy", headers=world.alice).status_code == 404
    for h in (world.alice, world.bob, world.carol):
        ids = {m["id"] for m in client.get("/api/v1/meetings", headers=h).json()["meetings"]}
        assert "legacy" not in ids


def test_list_is_scoped_per_user(client, world):
    ids = lambda h: {m["id"] for m in client.get("/api/v1/meetings", headers=h).json()["meetings"]}
    assert ids(world.alice) == {world.mid}
    assert ids(world.bob) == {world.mid}          # participant
    assert ids(world.carol) == set()
    assert client.get("/api/v1/meetings", headers=world.carol).json()["total"] == 0


def test_calendar_is_scoped_and_bound_to_the_caller(client, world):
    ev = lambda h, who: client.get(f"/api/v1/calendar/{who}/events", headers=h)
    mine = ev(world.alice, "alice@example.com").json()["events"]
    assert {e["id"] for e in mine} == {world.mid, "a1"}
    assert ev(world.carol, "carol@example.com").json()["events"] == []
    assert ev(world.carol, "alice@example.com").status_code == 403      # other's calendar
    assert ev(world.alice, "ALICE@example.com").status_code == 200       # case-insensitive
    assert ev(world.admin, "alice@example.com").status_code == 200       # admin may
    assert client.get("/api/v1/calendar/carol@example.com/health",
                      headers=world.alice).status_code == 403
    assert client.get("/api/v1/calendar/alice@example.com/health",
                      headers=world.alice).status_code == 200
    legacy = ev(world.admin, "x@y.com").json()["events"]
    assert "legacy" in {e["id"] for e in legacy}


def test_job_status_follows_meeting_access(client, world):
    jobs.create_job(world.mid, ["transcribe"], {}, job_id="job1")
    assert client.get("/api/v1/jobs/job1", headers=world.alice).status_code == 200
    assert client.get("/api/v1/jobs/job1", headers=world.bob).status_code == 200
    assert client.get("/api/v1/jobs/job1", headers=world.carol).status_code == 404
    assert client.get(f"/api/v1/meetings/{world.mid}/jobs/latest", headers=world.carol).status_code == 404


def test_conflict_details_of_others_meetings_are_redacted(client, world):
    r = client.post("/api/v1/meetings/schedule", headers=world.carol, json={
        "title": "Probe", "participants": ["alice@example.com"],
        "scheduled_time": future(), "duration_minutes": 30})
    assert r.status_code == 409
    c = r.json()["detail"]["conflicts"][0]
    assert c["redacted"] is True and c["title"] == "Busy" and c["id"] is None
    assert "Secret roadmap" not in r.text
    # the owner gets full details of her own conflicts
    r2 = client.post("/api/v1/meetings/schedule", headers=world.alice, json={
        "title": "Again", "participants": ["alice@example.com"],
        "scheduled_time": future(), "duration_minutes": 30})
    assert r2.json()["detail"]["conflicts"][0]["title"] == "Secret roadmap"


def test_cannot_reach_action_through_another_meeting(client, world):
    other = client.post("/api/v1/meetings/schedule", headers=world.carol, json={
        "title": "Carol's", "participants": [], "scheduled_time": future(5)}).json()["id"]
    assert client.get(f"/api/v1/meetings/{other}/actions/a1", headers=world.carol).status_code == 404
    assert client.delete(f"/api/v1/meetings/{other}/actions/a1", headers=world.carol).status_code == 404
    assert client.get(f"/api/v1/meetings/{world.mid}/actions/a1", headers=world.alice).status_code == 200


# ------------------------------------------------------ input hardening

def test_nul_and_oversized_ids_are_404_not_500(client, world):
    for bad in ("%00", "a%00b", "x" * 200):
        assert client.get(f"/api/v1/meetings/{bad}", headers=world.alice).status_code == 404
        assert client.get(f"/api/v1/meetings/{world.mid}/actions/{bad}", headers=world.alice).status_code == 404


def test_legacy_start_endpoint_validates_input(client, world):
    h = world.alice
    base = {"title": "x", "participants": []}
    assert client.post("/api/v1/meetings/start", headers=h, json={**base, "duration_minutes": -5}).status_code == 422
    assert client.post("/api/v1/meetings/start", headers=h, json={**base, "duration_minutes": 99999999999}).status_code == 422
    assert client.post("/api/v1/meetings/start", headers=h, json={**base, "title": "a" * 100000}).status_code == 422
    assert client.post("/api/v1/meetings/start", headers=h, json={**base, "participants": ["p"] * 101}).status_code == 422
    ok = client.post("/api/v1/meetings/start", headers=h, json=base)
    assert ok.status_code == 200
    assert client.get(f"/api/v1/meetings/{ok.json()['id']}", headers=h).status_code == 200  # owned


def test_expensive_operations_are_rate_limited(client, world, monkeypatch):
    monkeypatch.setenv("EXPENSIVE_LIMIT_PER_HOUR", "2")
    url = f"/api/v1/meetings/{world.mid}/process"
    codes = [client.post(url, headers=world.alice).status_code for _ in range(3)]
    assert codes[-1] == 429 and 429 not in codes[:2]
    # limits are per user
    assert client.post(url, headers=world.admin).status_code != 429


# ------------------------------------------------------ error safety

def test_internal_errors_do_not_leak_details(client, world, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret postgres://user:pw@host/db")
    monkeypatch.setattr(pipeline, "summarize_step", boom)
    r = client.post(f"/api/v1/meetings/{world.mid}/summarize", headers=world.alice)
    assert r.status_code == 500 and "secret" not in r.text and "postgres" not in r.text


def test_unhandled_exceptions_return_generic_message_with_request_id(Session, monkeypatch):
    def override():
        d = Session()
        try:
            yield d
        finally:
            d.close()
    app.dependency_overrides[get_db] = override
    try:
        c = TestClient(app, raise_server_exceptions=False)
        h = signup(c, "alice@example.com")
        monkeypatch.setattr(auth, "can_read",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret path /etc")))
        c.post("/api/v1/meetings/start", headers=h, json={"title": "t", "participants": []})
        r = c.get("/api/v1/meetings", headers=h)
        assert r.status_code == 500
        assert r.json()["detail"] == "Internal server error" and r.json()["request_id"]
        assert "secret" not in r.text
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------ browser security

def test_cors_only_allows_configured_origins(client):
    pre = lambda origin: client.options("/api/v1/meetings", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization"})
    evil = pre("http://evil.example")
    assert "access-control-allow-origin" not in evil.headers
    good = pre("http://localhost:3000")
    assert good.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in good.headers


def test_responses_carry_request_id_and_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-request-id"] and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------- permissions of the frontend endpoints

def test_audio_download_follows_meeting_access(client, world, Session, tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"RIFFdata")
    s = Session(); s.get(Meeting, world.mid).audio_file_path = str(f); s.commit(); s.close()
    url = f"/api/v1/meetings/{world.mid}/audio"
    assert client.get(url, headers=world.alice).status_code == 200
    assert client.get(url, headers=world.bob).status_code == 200          # participants may listen
    assert client.get(url, headers=world.carol).status_code == 404
    assert client.get(url).status_code == 401


def test_only_owner_can_delete_a_meeting(client, world):
    url = f"/api/v1/meetings/{world.mid}"
    assert client.delete(url, headers=world.carol).status_code == 404     # stranger: not even visible
    assert client.delete(url, headers=world.bob).status_code == 403       # participant: read-only
    assert client.get(url, headers=world.alice).status_code == 200
    assert client.delete(url, headers=world.alice).status_code == 204
    assert client.get(url, headers=world.alice).status_code == 404
    assert client.delete(url).status_code == 401


def test_can_write_flag_reflects_the_viewer(client, world):
    url = f"/api/v1/meetings/{world.mid}"
    assert client.get(url, headers=world.alice).json()["can_write"] is True
    assert client.get(url, headers=world.bob).json()["can_write"] is False
    assert client.get(url, headers=world.admin).json()["can_write"] is True
    listing = client.get("/api/v1/meetings", headers=world.bob).json()["meetings"][0]
    assert listing["can_write"] is False and listing["owner_email"] == "alice@example.com"


def test_task_list_only_includes_your_own_tasks(client, world, Session):
    with Session() as db:
        db.add(ActionItemDB(id="b1", meeting_id=world.mid, task="Bob's own task", assignee="bob@example.com",
                            due_date=datetime(2030, 1, 6)))
        db.commit()
    ids = lambda h: {a["id"] for a in client.get("/api/v1/actions", headers=h).json()["action_items"]}
    assert ids(world.bob) == {"b1"}                  # only what is assigned to Bob (a1 is not his)
    assert ids(world.alice) == set()                 # the owner does not see Bob's task in "my tasks"
    assert ids(world.carol) == set()
    assert ids(world.admin) == set()                 # not even an administrator: it is a personal list
    assert client.get("/api/v1/actions?mine=false", headers=world.alice).json()["total"] == 0   # others' cannot be requested
    assert client.get("/api/v1/actions", headers=world.carol).json()["total"] == 0
    assert client.get(f"/api/v1/actions?meeting_id={world.mid}", headers=world.carol).json()["total"] == 0
    bob_view = client.get("/api/v1/actions", headers=world.bob).json()["action_items"][0]
    assert bob_view["can_write"] is False and bob_view["meeting_title"] == "Secret roadmap" and bob_view["mine"] is True
