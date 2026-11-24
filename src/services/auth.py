"""
Authentication and authorization.

- Passwords: scrypt (stdlib) with a random per-user salt.
- Tokens: short-lived HS256 JWTs signed with JWT_SECRET (required; the
  application refuses to start without a strong secret).
- Admins: users whose e-mail is listed in the ADMIN_EMAILS environment
  variable. Admins can see every meeting, including legacy meetings that
  have no owner.
- Access to a meeting: owner or admin may read and modify; a listed
  participant may read. Everyone else gets 404 (existence is not revealed).
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from sqlalchemy.orm import Session

from ..models.database import (AuthSession, Meeting, PasswordResetToken,
                               RefreshToken, User)
from ..models.db import get_db
from ..workers import jobs

TOKEN_ALGORITHM = "HS256"
TOKEN_ISSUER = "meeting-intelligence-agent"
MIN_SECRET_LENGTH = 32
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET must be set to a random value of at least "
            f"{MIN_SECRET_LENGTH} characters."
        )
    return secret


def token_ttl_minutes() -> int:
    try:
        return max(1, int(os.getenv("ACCESS_TOKEN_MINUTES", "15")))
    except ValueError:
        return 15


def admin_emails() -> set:
    return {
        e.strip().lower()
        for e in os.getenv("ADMIN_EMAILS", "").split(",")
        if e.strip()
    }


def registration_enabled() -> bool:
    return os.getenv("ALLOW_REGISTRATION", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def is_admin(user: User) -> bool:
    return user.email.lower() in admin_emails()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid e-mail address")
    return email


def validate_password(password: str) -> None:
    if not (MIN_PASSWORD_LENGTH <= len(password or "") <= MAX_PASSWORD_LENGTH):
        raise HTTPException(
            status_code=422,
            detail=f"Password must be {MIN_PASSWORD_LENGTH}-"
            f"{MAX_PASSWORD_LENGTH} characters",
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# A hash to verify against when the e-mail is unknown, so that response
# time does not reveal whether an account exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def refresh_ttl_days() -> int:
    return _env_int("REFRESH_TOKEN_DAYS", 14)


def session_max_days() -> int:
    return _env_int("SESSION_MAX_DAYS", 30)


def reset_ttl_minutes() -> int:
    return _env_int("PASSWORD_RESET_MINUTES", 30)


def create_access_token(user: User, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "sid": session_id,
        "jti": uuid.uuid4().hex,            # unique per token
        "email": user.email,
        "iss": TOKEN_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=token_ttl_minutes()),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=TOKEN_ALGORITHM)


def decode_access_token(token: str, verify_exp: bool = True) -> dict:
    return jwt.decode(
        token,
        jwt_secret(),
        algorithms=[TOKEN_ALGORITHM],          # never accept "none"
        issuer=TOKEN_ISSUER,
        options={"require": ["exp", "iat", "sub", "iss", "sid"],
                 "verify_exp": verify_exp},
    )


# ---------------------------------------------------------------------------
# Sessions and refresh tokens
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.utcnow()


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


def _invalid_refresh() -> HTTPException:
    return HTTPException(status_code=401, detail="Invalid refresh token",
                         headers={"WWW-Authenticate": "Bearer"})


def _add_refresh_token(db: Session, session: AuthSession) -> str:
    plain = _new_token()
    now = _now()
    db.add(RefreshToken(
        id=str(uuid.uuid4()),
        session_id=session.id,
        token_hash=_hash_token(plain),
        created_at=now,
        expires_at=min(now + timedelta(days=refresh_ttl_days()), session.expires_at),
    ))
    return plain


def _token_response(user: User, session: AuthSession, refresh_plain: str) -> dict:
    return {
        "access_token": create_access_token(user, session.id),
        "refresh_token": refresh_plain,
        "token_type": "bearer",
        "expires_in": token_ttl_minutes() * 60,
        "refresh_expires_in": refresh_ttl_days() * 86400,
    }


def start_session(db: Session, user: User, ip: str = "", user_agent: str = "") -> dict:
    """Create a login session and return access + refresh tokens."""
    now = _now()
    session = AuthSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=session_max_days()),
        ip_address=(ip or "")[:64] or None,
        user_agent=(user_agent or "")[:200] or None,
    )
    db.add(session)
    refresh_plain = _add_refresh_token(db, session)
    db.commit()
    return _token_response(user, session, refresh_plain)


def rotate_refresh_token(db: Session, refresh_plain: str) -> dict:
    """
    Exchange a refresh token for a new access + refresh pair. The presented
    token becomes unusable. Presenting a token that was already used means
    it may have been stolen, so the whole session is revoked.
    """
    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_token(refresh_plain))
        .with_for_update()
        .first()
    )
    if row is None:
        raise _invalid_refresh()
    now = _now()
    session = db.get(AuthSession, row.session_id)
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        raise _invalid_refresh()

    if row.used_at is not None:
        session.revoked_at = now
        db.commit()
        logger.warning("Refresh-token reuse detected; session {} revoked", session.id)
        raise _invalid_refresh()
    if row.expires_at <= now:
        raise _invalid_refresh()

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise _invalid_refresh()

    row.used_at = now
    session.last_used_at = now
    new_plain = _add_refresh_token(db, session)
    db.commit()
    return _token_response(user, session, new_plain)


def revoke_session(db: Session, session_id: str, user_id: Optional[str] = None) -> bool:
    """Revoke one session (optionally only if it belongs to user_id)."""
    session = db.get(AuthSession, session_id)
    if session is None or (user_id and session.user_id != user_id):
        return False
    if session.revoked_at is None:
        session.revoked_at = _now()
        db.commit()
    return True


def revoke_all_sessions(db: Session, user_id: str) -> int:
    now = _now()
    count = (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .update({AuthSession.revoked_at: now}, synchronize_session=False)
    )
    db.commit()
    return count


def session_id_from_refresh_token(db: Session, refresh_plain: str) -> Optional[str]:
    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash_token(refresh_plain))
        .first()
    )
    return row.session_id if row else None


def purge_expired(db: Session) -> None:
    """Delete long-expired tokens and sessions (housekeeping)."""
    cutoff = _now() - timedelta(days=1)
    try:
        db.query(RefreshToken).filter(RefreshToken.expires_at < cutoff).delete()
        db.query(PasswordResetToken).filter(PasswordResetToken.expires_at < cutoff).delete()
        db.commit()
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# Password change / reset
# ---------------------------------------------------------------------------

def set_password(db: Session, user: User, new_password: str) -> None:
    validate_password(new_password)
    user.password_hash = hash_password(new_password)


def change_password(db: Session, user: User, current: str, new: str,
                    ip: str = "", user_agent: str = "") -> dict:
    """Verify the current password, set the new one, end every session and
    return a fresh session for the caller."""
    if not verify_password(current or "", user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if new == current:
        raise HTTPException(status_code=422,
                            detail="New password must differ from the current one")
    set_password(db, user, new)
    db.commit()
    revoke_all_sessions(db, user.id)
    return start_session(db, user, ip, user_agent)


def create_reset_token(db: Session, user: User) -> str:
    """Issue a single-use reset token (older unused ones stop working)."""
    now = _now()
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)
    plain = _new_token()
    db.add(PasswordResetToken(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token_hash=_hash_token(plain),
        created_at=now,
        expires_at=now + timedelta(minutes=reset_ttl_minutes()),
    ))
    db.commit()
    return plain


def consume_reset_token(db: Session, token: str, new_password: str) -> User:
    """Validate the token, set the new password, end every session."""
    validate_password(new_password)
    now = _now()
    row = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _hash_token(token))
        .with_for_update()
        .first()
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    row.used_at = now
    set_password(db, user, new_password)
    db.commit()
    revoke_all_sessions(db, user.id)
    return user


MAX_NAME_LENGTH = 100


def validate_name(name: Optional[str]) -> Optional[str]:
    """Collapse whitespace; reject empty-after-trim or overlong/control-char names."""
    if name is None:
        return None
    cleaned = " ".join(name.split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="Name cannot be empty")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=422, detail=f"Name must be at most {MAX_NAME_LENGTH} characters"
        )
    if any(not ch.isprintable() for ch in cleaned):
        raise HTTPException(status_code=422, detail="Name contains invalid characters")
    return cleaned


def register_user(
    db: Session, email: str, password: str, name: Optional[str] = None
) -> User:
    email = validate_email(email)
    validate_password(password)
    name = validate_name(name)
    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(status_code=409, detail="E-mail already registered")
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password(password),
        name=name,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409, detail="E-mail already registered")
    return user


def authenticate(db: Session, email: str, password: str) -> Optional[User]:
    user = (
        db.query(User).filter(User.email == normalize_email(email)).first()
    )
    stored = user.password_hash if user else _DUMMY_HASH
    ok = verify_password(password or "", stored)
    if user is None or not ok or not user.is_active:
        return None
    return user


# ---------------------------------------------------------------------------
# Rate limiting (Redis fixed window; fails open if Redis is unavailable)
# ---------------------------------------------------------------------------

def _incr(key: str, window: int) -> Optional[int]:
    try:
        r = jobs.client()
        count = r.incr(key)
        if count == 1:
            r.expire(key, window)
        return int(count)
    except Exception:
        logger.warning("Rate limiter unavailable; allowing request")
        return None


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_rate_limit(bucket: str, identity: str, limit: int, window: int) -> None:
    """Count one hit; raise 429 when the limit for the window is exceeded."""
    window_id = int(time.time() // window)
    count = _incr(f"rl:{bucket}:{identity}:{window_id}", window)
    if count is not None and count > limit:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(window)},
        )


def login_failures_exceeded(email: str, ip: str, limit: int = 5,
                            window: int = 900) -> bool:
    try:
        window_id = int(time.time() // window)
        value = jobs.client().get(f"rl:loginfail:{normalize_email(email)}:{ip}:{window_id}")
        return value is not None and int(value) >= limit
    except Exception:
        return False


def record_login_failure(email: str, ip: str, window: int = 900) -> None:
    _incr(f"rl:loginfail:{normalize_email(email)}:{ip}:{int(time.time() // window)}", window)


def clear_login_failures(email: str, ip: str, window: int = 900) -> None:
    try:
        jobs.client().delete(
            f"rl:loginfail:{normalize_email(email)}:{ip}:{int(time.time() // window)}"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        claims = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise unauthorized
    user = db.query(User).filter(User.id == claims["sub"]).first()
    if user is None or not user.is_active:
        raise unauthorized
    session = db.get(AuthSession, claims["sid"])
    if (
        session is None
        or session.user_id != user.id
        or session.revoked_at is not None
        or session.expires_at <= _now()
    ):
        raise unauthorized                  # logged out / revoked / expired
    user.current_session_id = session.id    # transient attribute for handlers
    return user


def limit_expensive(user: User = Depends(get_current_user)) -> User:
    """Authenticated user, with a per-hour cap on costly operations
    (upload, transcription, summarization, extraction, background jobs)."""
    try:
        limit = int(os.getenv("EXPENSIVE_LIMIT_PER_HOUR", "30"))
    except ValueError:
        limit = 30
    check_rate_limit("expensive", user.id, limit, 3600)
    return user


# ---------------------------------------------------------------------------
# Meeting authorization
# ---------------------------------------------------------------------------

def _participants(meeting: Meeting) -> set:
    return {p.strip().lower() for p in (meeting.participants or []) if isinstance(p, str)}


def is_owner(user: User, meeting: Meeting) -> bool:
    return bool(meeting.owner_email) and meeting.owner_email.lower() == user.email.lower()


def can_read(user: User, meeting: Meeting) -> bool:
    return (
        is_admin(user)
        or is_owner(user, meeting)
        or user.email.lower() in _participants(meeting)
    )


def can_write(user: User, meeting: Meeting) -> bool:
    return is_admin(user) or is_owner(user, meeting)


def authorize_meeting(user: User, meeting: Optional[Meeting], write: bool = False) -> Meeting:
    """Return the meeting, or raise 404 (no access) / 403 (read-only)."""
    if meeting is None or not can_read(user, meeting):
        raise HTTPException(status_code=404, detail="Meeting not found")
    if write and not can_write(user, meeting):
        raise HTTPException(
            status_code=403,
            detail="Only the meeting owner can modify this meeting",
        )
    return meeting
