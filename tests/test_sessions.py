"""Sessions, logout, revocation, refresh-token rotation, password change/reset."""

import re
import smtplib
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import cli
from src.api.main import app
from src.models.database import (AuthSession, Base, PasswordResetToken,
                                 RefreshToken, User)
from src.models.db import get_db
from src.services import auth, mailer

PW = "correct horse battery"
NEW_PW = "another long passphrase"


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
    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(client, email="alice@example.com", password=PW, register=True, ua=None):
    if register:
        client.post("/auth/register", json={"email": email, "password": password})
    r = client.post("/auth/login", json={"email": email, "password": password},
                    headers={"User-Agent": ua} if ua else {})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def me(client, tokens):
    return client.get("/auth/me", headers=bearer(tokens)).status_code


# ------------------------------------------------------------------ login

def test_login_returns_access_and_refresh_tokens(client):
    t = login(client)
    assert set(t) >= {"access_token", "refresh_token", "token_type", "expires_in",
                      "refresh_expires_in"}
    assert t["expires_in"] == 15 * 60 and t["refresh_expires_in"] == 14 * 86400
    claims = jwt.decode(t["access_token"], options={"verify_signature": False})
    assert claims["sid"]
    assert me(client, t) == 200


def test_refresh_tokens_are_stored_hashed(client, Session):
    t = login(client)
    s = Session()
    row = s.query(RefreshToken).one()
    assert row.token_hash != t["refresh_token"] and t["refresh_token"] not in row.token_hash
    assert len(row.token_hash) == 64
    s.close()


# ----------------------------------------------------------------- logout

def test_logout_invalidates_access_token_immediately(client):
    t = login(client)
    assert client.post("/auth/logout", headers=bearer(t)).status_code == 204
    assert me(client, t) == 401                      # same, still-unexpired token
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_logout_with_only_refresh_token_when_access_expired(client):
    t = login(client)
    r = client.post("/auth/logout", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 204
    assert me(client, t) == 401


def test_logout_with_expired_access_token_still_revokes(client, Session):
    t = login(client)
    claims = jwt.decode(t["access_token"], options={"verify_signature": False})
    expired = jwt.encode({**claims, "exp": int((datetime.utcnow() - timedelta(minutes=5)).timestamp())},
                         __import__("os").environ["JWT_SECRET"], "HS256")
    assert client.post("/auth/logout", headers={"Authorization": f"Bearer {expired}"}).status_code == 204
    s = Session(); assert s.query(AuthSession).one().revoked_at is not None; s.close()


def test_logout_is_idempotent_and_reveals_nothing(client):
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/logout", headers={"Authorization": "Bearer garbage"}).status_code == 204
    assert client.post("/auth/logout", json={"refresh_token": "x" * 40}).status_code == 204


def test_logout_only_affects_one_session(client):
    t1 = login(client, ua="laptop")
    t2 = login(client, register=False, ua="phone")
    client.post("/auth/logout", headers=bearer(t1))
    assert me(client, t1) == 401 and me(client, t2) == 200


def test_logout_all_signs_out_every_device(client):
    t1, t2 = login(client), None
    t2 = login(client, register=False)
    assert client.post("/auth/logout-all", headers=bearer(t1)).status_code == 204
    assert me(client, t1) == 401 and me(client, t2) == 401
    assert client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    # signing in again works
    assert me(client, login(client, register=False)) == 200


def test_logout_all_does_not_touch_other_users(client):
    a, b = login(client, "a@example.com"), login(client, "b@example.com")
    client.post("/auth/logout-all", headers=bearer(a))
    assert me(client, b) == 200


# ---------------------------------------------------------------- sessions

def test_list_and_revoke_sessions(client):
    t1 = login(client, ua="laptop-browser")
    t2 = login(client, register=False, ua="phone-app")
    rows = client.get("/auth/sessions", headers=bearer(t1)).json()["sessions"]
    assert len(rows) == 2
    assert [r["user_agent"] for r in rows if r["current"]] == ["laptop-browser"]
    other = [r for r in rows if not r["current"]][0]
    assert client.delete(f"/auth/sessions/{other['id']}", headers=bearer(t1)).status_code == 204
    assert me(client, t2) == 401 and me(client, t1) == 200
    assert len(client.get("/auth/sessions", headers=bearer(t1)).json()["sessions"]) == 1


def test_cannot_revoke_someone_elses_session(client):
    a, b = login(client, "a@example.com"), login(client, "b@example.com")
    sid = client.get("/auth/sessions", headers=bearer(b)).json()["sessions"][0]["id"]
    assert client.delete(f"/auth/sessions/{sid}", headers=bearer(a)).status_code == 404
    assert me(client, b) == 200
    assert client.delete("/auth/sessions/nope", headers=bearer(a)).status_code == 404


# ----------------------------------------------------------------- refresh

def test_refresh_rotates_tokens(client):
    t = login(client)
    r = client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 200
    n = r.json()
    assert n["refresh_token"] != t["refresh_token"] and n["access_token"] != t["access_token"]
    assert me(client, n) == 200
    assert me(client, t) == 200                  # old access token lives out its short life
    # same session throughout
    assert jwt.decode(n["access_token"], options={"verify_signature": False})["sid"] == \
        jwt.decode(t["access_token"], options={"verify_signature": False})["sid"]


def test_reusing_a_refresh_token_revokes_the_session(client):
    t = login(client)
    n = client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
    # an attacker (or buggy client) replays the old token:
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    # ... which kills the legitimate session too
    assert me(client, n) == 401
    assert client.post("/auth/refresh", json={"refresh_token": n["refresh_token"]}).status_code == 401


def test_refresh_rejects_garbage_expired_and_revoked(client, Session):
    assert client.post("/auth/refresh", json={"refresh_token": "g" * 40}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": "short"}).status_code == 422
    t = login(client)
    s = Session()
    s.query(RefreshToken).update({RefreshToken.expires_at: datetime.utcnow() - timedelta(seconds=1)})
    s.commit(); s.close()
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_refresh_stops_after_absolute_session_lifetime(client, Session):
    t = login(client)
    s = Session()
    s.query(AuthSession).update({AuthSession.expires_at: datetime.utcnow() - timedelta(seconds=1)})
    s.commit(); s.close()
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert me(client, t) == 401


def test_refresh_token_never_outlives_its_session(client, Session, monkeypatch):
    monkeypatch.setenv("SESSION_MAX_DAYS", "1")
    monkeypatch.setenv("REFRESH_TOKEN_DAYS", "14")
    login(client)
    s = Session()
    sess, tok = s.query(AuthSession).one(), s.query(RefreshToken).one()
    assert tok.expires_at <= sess.expires_at
    s.close()


def test_deactivated_user_cannot_refresh(client, Session):
    t = login(client)
    s = Session(); s.query(User).update({User.is_active: False}); s.commit(); s.close()
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert me(client, t) == 401


def test_session_of_other_user_token_mismatch(client, Session):
    """An access token whose sid belongs to a different user is rejected."""
    a, b = login(client, "a@example.com"), login(client, "b@example.com")
    sid_b = jwt.decode(b["access_token"], options={"verify_signature": False})["sid"]
    claims = jwt.decode(a["access_token"], options={"verify_signature": False})
    forged = jwt.encode({**claims, "sid": sid_b}, __import__("os").environ["JWT_SECRET"], "HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_tokens_without_session_claim_are_rejected(client):
    t = login(client)
    claims = jwt.decode(t["access_token"], options={"verify_signature": False})
    claims.pop("sid")
    old_style = jwt.encode(claims, __import__("os").environ["JWT_SECRET"], "HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old_style}"}).status_code == 401


# ---------------------------------------------------------- change password

def test_change_password_ends_all_other_sessions_and_keeps_this_one(client):
    t1 = login(client)
    t2 = login(client, register=False)
    r = client.post("/auth/change-password", headers=bearer(t1),
                    json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200
    fresh = r.json()
    assert me(client, t1) == 401 and me(client, t2) == 401      # all old sessions gone
    assert client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert me(client, fresh) == 200                              # caller stays signed in
    # old password no longer works, new one does
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PW}).status_code == 401
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": NEW_PW}).status_code == 200


def test_change_password_validation(client):
    t = login(client)
    h = bearer(t)
    assert client.post("/auth/change-password", headers=h,
                       json={"current_password": "wrong password!!", "new_password": NEW_PW}).status_code == 401
    assert client.post("/auth/change-password", headers=h,
                       json={"current_password": PW, "new_password": PW}).status_code == 422
    assert client.post("/auth/change-password", headers=h,
                       json={"current_password": PW, "new_password": "short"}).status_code == 422
    assert me(client, t) == 200                                  # nothing changed
    assert client.post("/auth/change-password",
                       json={"current_password": PW, "new_password": NEW_PW}).status_code == 401


def test_change_password_brute_force_is_throttled(client):
    t = login(client)
    for _ in range(5):
        client.post("/auth/change-password", headers=bearer(t),
                    json={"current_password": "wrong password!!", "new_password": NEW_PW})
    r = client.post("/auth/change-password", headers=bearer(t),
                    json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 429


# ------------------------------------------------------------ password reset

@pytest.fixture
def outbox(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): self.args = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("EMAIL_ENABLED", "true")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "noreply@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    return sent


def token_from(msg):
    return re.search(r"([A-Za-z0-9_\-]{40,})", msg.get_content()).group(1)


def test_forgot_reset_flow_end_to_end(client, outbox):
    t_old = login(client)
    r = client.post("/auth/forgot-password", json={"email": "Alice@Example.com"})
    assert r.status_code == 202
    assert len(outbox) == 1 and outbox[0]["To"] == "alice@example.com"
    token = token_from(outbox[0])
    assert "30 minutes" in outbox[0].get_content()

    r = client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PW})
    assert r.status_code == 200
    assert me(client, t_old) == 401                              # all sessions ended
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": PW}).status_code == 401
    assert client.post("/auth/login", json={"email": "alice@example.com", "password": NEW_PW}).status_code == 200
    # single use
    assert client.post("/auth/reset-password", json={"token": token, "new_password": "yet another pass1"}).status_code == 400


def test_forgot_password_does_not_reveal_whether_account_exists(client, outbox):
    login(client)
    known = client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    unknown = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(outbox) == 1                                      # only the real account got mail


def test_forgot_password_validates_email_and_throttles(client, outbox):
    assert client.post("/auth/forgot-password", json={"email": "not-an-email"}).status_code == 422
    login(client)
    codes = [client.post("/auth/forgot-password", json={"email": "alice@example.com"}).status_code
             for _ in range(4)]
    assert codes == [202, 202, 202, 429]


def test_newer_reset_token_invalidates_older(client, outbox):
    login(client)
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    first, second = token_from(outbox[0]), token_from(outbox[1])
    assert client.post("/auth/reset-password", json={"token": first, "new_password": NEW_PW}).status_code == 400
    assert client.post("/auth/reset-password", json={"token": second, "new_password": NEW_PW}).status_code == 200


def test_reset_token_expiry_and_stored_hashed(client, outbox, Session):
    login(client)
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    token = token_from(outbox[0])
    s = Session()
    row = s.query(PasswordResetToken).one()
    assert token not in row.token_hash and len(row.token_hash) == 64
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    s.commit(); s.close()
    assert client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PW}).status_code == 400


def test_reset_rejects_bad_tokens_and_weak_passwords(client, outbox):
    login(client)
    assert client.post("/auth/reset-password", json={"token": "g" * 40, "new_password": NEW_PW}).status_code == 400
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    token = token_from(outbox[0])
    assert client.post("/auth/reset-password", json={"token": token, "new_password": "short"}).status_code == 422
    # a weak-password attempt does not burn the token
    assert client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PW}).status_code == 200


def test_email_disabled_sends_nothing_but_cli_can_issue_token(client, monkeypatch, Session):
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    sent = []
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: sent.append(1))
    login(client)
    assert client.post("/auth/forgot-password", json={"email": "alice@example.com"}).status_code == 202
    assert sent == [] and mailer.send_email("a@b.c", "s", "b") is False


def test_cli_reset_token_works(client, monkeypatch, Session, capsys):
    t = login(client)
    monkeypatch.setattr(cli, "SessionLocal", Session)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    assert cli.main(["reset-token", "alice@example.com"]) == 0
    token = re.search(r"\n([A-Za-z0-9_\-]{40,})", capsys.readouterr().out).group(1)
    assert client.post("/auth/reset-password", json={"token": token, "new_password": NEW_PW}).status_code == 200
    assert me(client, t) == 401
    assert cli.main(["reset-token", "ghost@example.com"]) == 1
    login(client, password=NEW_PW, register=False)
    assert cli.main(["revoke-sessions", "alice@example.com"]) == 0


def test_mailer_failure_never_raises_or_logs_the_token(monkeypatch, outbox):
    def boom(*a, **k): raise OSError("connection refused")
    monkeypatch.setattr(smtplib, "SMTP", boom)
    assert mailer.send_password_reset("a@example.com", "SECRET-TOKEN", 30) is False


def test_reset_link_uses_configured_frontend_url(monkeypatch, outbox):
    monkeypatch.setenv("PASSWORD_RESET_URL", "https://app.example.test/reset")
    assert mailer.send_password_reset("a@example.com", "T" * 48, 30)
    assert "https://app.example.test/reset?token=" + "T" * 48 in outbox[0].get_content()


def test_old_login_response_fields_still_present(client):
    t = login(client)
    assert t["token_type"] == "bearer" and t["access_token"]


# ------------------------------------------------ configurable rate limits

def test_registration_limit_is_configurable(client, monkeypatch):
    monkeypatch.setenv("REGISTER_LIMIT_PER_HOUR", "2")
    codes = [client.post("/auth/register", json={"email": f"u{i}@example.com", "password": PW}).status_code
             for i in range(3)]
    assert codes == [201, 201, 429]


def test_registration_limit_has_a_safe_default_and_ignores_garbage(client, monkeypatch):
    monkeypatch.setenv("REGISTER_LIMIT_PER_HOUR", "not-a-number")
    codes = [client.post("/auth/register", json={"email": f"d{i}@example.com", "password": PW}).status_code
             for i in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429     # falls back to 10


def test_refresh_limit_is_configurable_and_defaults_to_a_reload_friendly_value(client, monkeypatch):
    t = login(client)
    monkeypatch.setenv("REFRESH_LIMIT_PER_HOUR", "2")
    token = t["refresh_token"]
    codes = []
    for _ in range(3):
        r = client.post("/auth/refresh", json={"refresh_token": token})
        codes.append(r.status_code)
        if r.status_code == 200:
            token = r.json()["refresh_token"]
    assert codes == [200, 200, 429]
    # default: many reloads in an hour must not lock a user out
    monkeypatch.delenv("REFRESH_LIMIT_PER_HOUR")
    from src.workers import jobs
    jobs.set_client(type(jobs.client())())            # fresh counters
    t2 = login(client, register=False)
    tok = t2["refresh_token"]
    for _ in range(30):
        r = client.post("/auth/refresh", json={"refresh_token": tok})
        assert r.status_code == 200
        tok = r.json()["refresh_token"]
