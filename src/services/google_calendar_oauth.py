import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import redis
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from ..models.database import CalendarAccount
from .token_encryption import encrypt_token


GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/calendar",
]

OAUTH_STATE_TTL_SECONDS = 600


def _redis_client() -> redis.Redis:
    """Create a Redis client using the configured Redis URL."""
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return redis.Redis.from_url(redis_url, decode_responses=True)


def _google_config() -> tuple[dict[str, Any], str, str]:
    """Load Google OAuth configuration, rejecting placeholder credentials."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/auth/google/callback",
    ).strip()

    placeholders = {
        "",
        "your_client_id_here",
        "your_secret_here",
        "your_client_secret_here",
    }
    if client_id.lower() in placeholders or client_secret.lower() in placeholders:
        raise RuntimeError(
            "Google OAuth credentials are not configured. Set valid "
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET values."
        )

    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return config, redirect_uri, client_id


def create_google_authorization() -> str:
    """Create a Google authorization URL and store one-time state in Redis."""
    config, redirect_uri, _ = _google_config()
    state = secrets.token_urlsafe(32)

    flow = Flow.from_client_config(
        config,
        scopes=GOOGLE_SCOPES,
        state=state,
    )
    flow.redirect_uri = redirect_uri

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    saved = _redis_client().set(
        f"oauth:google:{state}",
        "pending",
        ex=OAUTH_STATE_TTL_SECONDS,
        nx=True,
    )
    if not saved:
        raise RuntimeError("Could not save OAuth state. Please retry.")

    return authorization_url


def consume_google_state(state: str) -> bool:
    """Validate and consume an OAuth state exactly once."""
    if not state:
        return False

    client = _redis_client()
    key = f"oauth:google:{state}"

    # Atomic read-and-delete prevents a state value from being reused.
    result = client.eval(
        """
        local value = redis.call('GET', KEYS[1])
        if value then
            redis.call('DEL', KEYS[1])
        end
        return value
        """,
        1,
        key,
    )
    return result == "pending"


def complete_google_authorization(
    state: str,
    code: str,
    db: Session,
) -> dict[str, Any]:
    """
    Complete Google OAuth, verify the account, and save encrypted tokens.

    The OAuth state is consumed before exchanging the code so that callbacks
    cannot be replayed. This application currently has no separate signed-in
    app-user session, so the verified Google email is used as the account
    owner identifier.
    """
    if not code:
        raise ValueError("Missing Google authorization code.")

    if not consume_google_state(state):
        raise ValueError("Invalid, expired, or already-used OAuth state.")

    config, redirect_uri, client_id = _google_config()
    flow = Flow.from_client_config(
        config,
        scopes=GOOGLE_SCOPES,
        state=state,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)

    credentials = flow.credentials
    if not credentials.id_token:
        raise ValueError(
            "Google did not return an ID token. Check the requested OAuth scopes."
        )

    identity = id_token.verify_oauth2_token(
        credentials.id_token,
        Request(),
        audience=client_id,
    )

    email = identity.get("email")
    google_subject = identity.get("sub")
    if not email or not google_subject or not identity.get("email_verified", False):
        raise ValueError("Google account identity could not be verified.")

    if not credentials.token:
        raise ValueError("Google did not return an access token.")

    expires_at = None
    if credentials.expiry:
        expiry = credentials.expiry
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        expires_at = expiry

    existing = (
        db.query(CalendarAccount)
        .filter(
            CalendarAccount.provider == "google",
            CalendarAccount.provider_account_id == google_subject,
        )
        .first()
    )

    encrypted_access_token = encrypt_token(credentials.token)
    encrypted_refresh_token = (
        encrypt_token(credentials.refresh_token)
        if credentials.refresh_token
        else None
    )

    if existing:
        existing.user_email = email
        existing.access_token_encrypted = encrypted_access_token
        # Google may omit a refresh token on reconnect; preserve the old one.
        if encrypted_refresh_token:
            existing.refresh_token_encrypted = encrypted_refresh_token
        existing.token_expires_at = expires_at
        existing.scopes = list(credentials.scopes or GOOGLE_SCOPES)
        existing.is_active = True
        existing.updated_at = datetime.utcnow()
        account = existing
    else:
        if not encrypted_refresh_token:
            raise ValueError(
                "Google did not return a refresh token. Revoke the app's access "
                "in your Google Account and retry authorization with consent."
            )

        account = CalendarAccount(
            id=secrets.token_urlsafe(24),
            user_email=email,
            provider="google",
            provider_account_id=google_subject,
            access_token_encrypted=encrypted_access_token,
            refresh_token_encrypted=encrypted_refresh_token,
            token_expires_at=expires_at,
            scopes=list(credentials.scopes or GOOGLE_SCOPES),
            is_active=True,
        )
        db.add(account)

    try:
        db.commit()
        db.refresh(account)
    except Exception:
        db.rollback()
        raise

    return {
        "status": "connected",
        "provider": "google",
        "user_email": account.user_email,
        "account_id": account.id,
        "scopes": account.scopes or [],
        "token_expires_at": (
            account.token_expires_at.isoformat()
            if account.token_expires_at
            else None
        ),
    }
