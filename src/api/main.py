import asyncio
import math
import os
import re
import uuid
import logging

from datetime import date, datetime, timezone, timedelta
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Request,
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Form,
    Depends,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..services import audio_storage, internal_calendar, scheduling, pipeline, mailer, voices, assignments, transcript_edit, ask, search as search_service, ical, summary_email, analytics, notifications, reminders, timeutil, task_thread, recurrence, folders
from ..agent.qa import MeetingQA, QAError
from ..agent.transcriber import DiarizationUnavailableError, NoSpeechError
from ..models.database import AskHistory, Notification, TaskComment, VoiceProfile
from ..models.database import AuthSession
from ..agent.scheduler import NoAvailableSlotError
from ..workers import jobs as job_store
from ..services import auth
from ..services.auth import get_current_user, limit_expensive
from ..models.database import User
from starlette.concurrency import run_in_threadpool
from contextvars import ContextVar

logger = logging.getLogger(__name__)

from ..agent import (
    MeetingTranscriber,
    MeetingSummarizer,
    ActionItemExtractor,
    CalendarOptimizer,
)
from ..models.schemas import (
    MeetingCreate,
    MeetingResponse,
    FindTimeRequest,
    TranscriptionResult,
)
from ..models.database import (Folder, 
    Meeting,
    Transcript,
    Summary,
    ActionItemDB,
    CalendarAccount,
)
from ..models.db import get_db, init_db

# Initialize FastAPI app
_docs_on = os.getenv("ENABLE_DOCS", "true").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="Meeting Intelligence Agent API",
    description="AI-powered meeting transcription, summarization, and scheduling",
    version="1.0.0",
    docs_url="/docs" if _docs_on else None,
    redoc_url="/redoc" if _docs_on else None,
    openapi_url="/openapi.json" if _docs_on else None,
)

# Every /api/v1 route is registered on this router and therefore requires
# a valid bearer token (default deny). Public routes are defined on `app`.
api = APIRouter(dependencies=[Depends(get_current_user)])

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id.get()
        return True


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, _RequestIdFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.addFilter(_RequestIdFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
        root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


_configure_logging()


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:12]
    token = _request_id.set(rid)
    try:
        response = await call_next(request)
    finally:
        _request_id.reset(token)
    response.headers["X-Request-ID"] = rid
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    rid = uuid.uuid4().hex[:12]
    logger.error("Unhandled error [%s] on %s %s", rid, request.method,
                 request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": rid},
        headers={"X-Request-ID": rid},
    )



# Google Calendar OAuth endpoints (disabled unless ENABLE_GOOGLE_OAUTH=true).
# The application's calendar is internal; these routes exist only for
# previously connected accounts and are not registered by default.

def _register_google_oauth() -> None:
    from ..services.google_calendar_oauth import (
        create_google_authorization,
        complete_google_authorization,
    )


    @app.get("/auth/google/login")
    async def google_login():
        """Start Google Calendar OAuth authorization."""
        try:
            authorization_url = create_google_authorization()
            return {"authorization_url": authorization_url}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))


    @app.get("/auth/google/callback")
    async def google_callback(
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
        db: Session = Depends(get_db),
    ):
        if error:
            raise HTTPException(
                status_code=400,
                detail="Google authorization was not completed.",
            )

        if not code or not state:
            raise HTTPException(
                status_code=400,
                detail="Missing Google authorization code or state.",
            )

        try:
            return complete_google_authorization(
                state=state,
                code=code,
                db=db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            db.rollback()
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception:
            db.rollback()
            logger.exception("Google OAuth callback failed")
            raise HTTPException(
                status_code=502,
                detail="Google authorization could not be completed. Check server logs.",
            )


if os.getenv("ENABLE_GOOGLE_OAUTH", "false").strip().lower() in {"1", "true", "yes", "on"}:
    _register_google_oauth()


@app.on_event("startup")
def initialize_database():
    """Create missing database tables at application startup."""
    auth.jwt_secret()   # fail closed: refuse to start without a strong secret
    init_db()


def run_reminder_cycle() -> int:
    """One pass: create due reminders, then send e-mail copies to people who asked for them."""
    from ..models.db import SessionLocal
    db = SessionLocal()
    try:
        created = reminders.scan(db)
        notifications.deliver_emails(db)
        return created
    finally:
        db.close()


async def _reminder_loop(interval: int) -> None:
    await asyncio.sleep(20)                       # let the server finish starting
    while True:
        try:
            created = await run_in_threadpool(run_reminder_cycle)
            if created:
                logger.info("Created {} reminder notifications", created)
        except Exception:
            logger.exception("Reminder cycle failed")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def start_reminders():
    """Deadline and meeting reminders run in the background (REMINDERS_ENABLED=false turns them off)."""
    if os.getenv("REMINDERS_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        interval = max(30, int(os.getenv("REMINDER_SCAN_SECONDS", "300")))
    except ValueError:
        interval = 300
    app.state.reminder_task = asyncio.create_task(_reminder_loop(interval))


# CORS middleware
_cors_origins = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip() and o.strip() != "*"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,   # tokens travel in the Authorization header
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# Initialize agents
transcriber = MeetingTranscriber(
    model=os.getenv("WHISPER_MODEL", "base"),
    device=os.getenv("WHISPER_DEVICE", "cpu"),
    compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
)

ollama_host = os.getenv(
    "OLLAMA_HOST",
    "http://host.docker.internal:11434",
)
ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2")

summarizer = MeetingSummarizer(
    ollama_host=ollama_host,
    model=ollama_model,
)

qa = MeetingQA(
    ollama_host=ollama_host,
    model=ollama_model,
)

action_extractor = ActionItemExtractor(
    ollama_host=ollama_host,
    model=ollama_model,
)

scheduler = CalendarOptimizer()


# General endpoints

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Meeting Intelligence Agent API",
        "version": "1.0.0",
        "features": [
            "transcription",
            "summarization",
            "action_items",
            "scheduling",
        ],
        "docs": "/docs",
    }


@app.get("/health")
async def health_check():
    """Health check."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/health/ready")
async def readiness_check(response: Response):
    """
    Readiness: can this instance serve traffic?

    Database and Redis are required (503 if either is down). Ollama is
    optional for serving requests, so its absence reports "degraded".
    No details beyond ok/fail are exposed.
    """
    import httpx
    from sqlalchemy import text

    checks = {}
    try:
        from ..models.db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "fail"
    try:
        job_store.client().ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "fail"
    try:
        host, _model = pipeline.ollama_settings()
        async with httpx.AsyncClient(timeout=2.0) as http:
            (await http.get(f"{host.rstrip('/')}/api/tags")).raise_for_status()
        checks["ollama"] = "ok"
    except Exception:
        checks["ollama"] = "fail"

    if "fail" in (checks["database"], checks["redis"]):
        response.status_code = 503
        status = "not_ready"
    else:
        status = "ready" if checks["ollama"] == "ok" else "degraded"
    return {"status": status, "checks": checks}


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


class RegisterRequest(Credentials):
    # Optional for API compatibility; the web form requires it.
    name: Optional[str] = Field(default=None, max_length=200)


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    timezone: Optional[str] = Field(default=None, max_length=64)      # "" clears it
    email_notifications: Optional[bool] = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=256)


class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = Field(default=None, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(max_length=254)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


@app.post("/auth/register", status_code=201)
async def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create an account (can be disabled with ALLOW_REGISTRATION=false)."""
    if not auth.registration_enabled():
        raise HTTPException(status_code=403, detail="Registration is disabled")
    try:
        register_limit = int(os.getenv("REGISTER_LIMIT_PER_HOUR", "10"))
    except ValueError:
        register_limit = 10
    auth.check_rate_limit("register", auth.client_ip(request), register_limit, 3600)
    user = auth.register_user(db, body.email, body.password, body.name)
    return {"id": user.id, "email": user.email, "name": user.name}


@app.post("/auth/login")
async def login(
    body: Credentials,
    request: Request,
    db: Session = Depends(get_db),
):
    """Sign in: returns a short-lived access token and a rotating refresh token."""
    ip = auth.client_ip(request)
    if auth.login_failures_exceeded(body.email, ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": "900"},
        )
    user = auth.authenticate(db, body.email, body.password)
    if user is None:
        auth.record_login_failure(body.email, ip)
        raise HTTPException(
            status_code=401,
            detail="Invalid e-mail or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    auth.clear_login_failures(body.email, ip)
    auth.purge_expired(db)
    return auth.start_session(db, user, ip, request.headers.get("user-agent", ""))


@app.post("/auth/refresh")
async def refresh(
    body: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Exchange a refresh token for a new access + refresh token. Each refresh
    token works once; send the newest one next time. Reusing an old one
    revokes the whole session.
    """
    try:
        refresh_limit = int(os.getenv("REFRESH_LIMIT_PER_HOUR", "600"))
    except ValueError:
        refresh_limit = 600
    auth.check_rate_limit("refresh", auth.client_ip(request), refresh_limit, 3600)
    return auth.rotate_refresh_token(db, body.refresh_token)


@app.post("/auth/logout", status_code=204)
async def logout(
    body: Optional[LogoutRequest] = None,
    credentials=Depends(auth.bearer_scheme),
    db: Session = Depends(get_db),
):
    """
    End the current session. Works with the access token, the refresh token
    or both (so a client whose access token already expired can still sign
    out). Always 204: it reveals nothing about token validity.
    """
    if credentials is not None:
        try:
            claims = auth.decode_access_token(credentials.credentials, verify_exp=False)
            auth.revoke_session(db, claims["sid"], user_id=claims["sub"])
        except auth.jwt.PyJWTError:
            pass
    if body and body.refresh_token:
        sid = auth.session_id_from_refresh_token(db, body.refresh_token)
        if sid:
            auth.revoke_session(db, sid)
    return Response(status_code=204)


@app.post("/auth/logout-all", status_code=204)
async def logout_all(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sign out everywhere: revoke every session of the current user."""
    auth.revoke_all_sessions(db, user.id)
    return Response(status_code=204)


@app.get("/auth/sessions")
async def list_sessions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active sessions (devices) of the current user."""
    now = datetime.utcnow()
    rows = (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now)
        .order_by(AuthSession.created_at.desc())
        .all()
    )
    return {
        "sessions": [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat(),
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "expires_at": r.expires_at.isoformat(),
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "current": r.id == user.current_session_id,
            }
            for r in rows
        ]
    }


@app.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_one_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke one of your own sessions (e.g. a lost device)."""
    if not auth.revoke_session(db, session_id, user_id=user.id):
        raise HTTPException(status_code=404, detail="Session not found")
    return Response(status_code=204)


@app.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Change your password. Every session is ended (all devices are signed
    out) and a fresh token pair is returned for this one.
    """
    ip = auth.client_ip(request)
    if auth.login_failures_exceeded(user.email, ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.",
                            headers={"Retry-After": "900"})
    try:
        tokens = auth.change_password(
            db, user, body.current_password, body.new_password,
            ip, request.headers.get("user-agent", ""),
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            auth.record_login_failure(user.email, ip)
        raise
    auth.clear_login_failures(user.email, ip)
    return tokens


@app.post("/auth/forgot-password", status_code=202)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Request a password-reset e-mail. The answer is identical whether or not
    the account exists. If e-mail is not configured (EMAIL_ENABLED), an
    administrator can issue a token with `python -m src.cli reset-token`.
    """
    email = auth.validate_email(body.email)
    auth.check_rate_limit("forgot-ip", auth.client_ip(request), 10, 3600)
    auth.check_rate_limit("forgot-email", email, 3, 3600)
    user = db.query(User).filter(User.email == email).first()
    if user is not None and user.is_active:
        token = auth.create_reset_token(db, user)
        background.add_task(
            mailer.send_password_reset, user.email, token, auth.reset_ttl_minutes()
        )
    return {
        "detail": "If an account exists for that address, password reset "
        "instructions have been sent."
    }


@app.post("/auth/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Set a new password with a reset token. Ends all sessions."""
    auth.check_rate_limit("reset", auth.client_ip(request), 10, 3600)
    auth.consume_reset_token(db, body.token, body.new_password)
    return {"detail": "Password updated. Please sign in again."}


def _me_view(user: User, row: User) -> dict:
    return {
        "id": row.id,
        "email": row.email,
        "name": row.name,
        "timezone": row.timezone,
        "email_notifications": bool(row.email_notifications),
        "is_admin": auth.is_admin(user),
        "session_id": user.current_session_id,
    }


@app.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return _me_view(user, user)


@app.patch("/auth/me")
async def update_me(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the signed-in user's name, time zone or e-mail-notification choice."""
    if body.name is None and body.timezone is None and body.email_notifications is None:
        raise HTTPException(status_code=422, detail="Nothing to change")
    row = db.query(User).filter(User.id == user.id).one()
    if body.name is not None:
        row.name = auth.validate_name(body.name)
    if body.timezone is not None:
        tz = body.timezone.strip()
        if tz and not timeutil.is_valid_tz(tz):
            raise HTTPException(status_code=422, detail="Unknown time zone. Use a name like Asia/Kolkata or Europe/London.")
        row.timezone = tz or None
    if body.email_notifications is not None:
        row.email_notifications = body.email_notifications
    db.commit()
    return _me_view(user, row)


# Meeting endpoints

@api.post("/api/v1/meetings/start", response_model=MeetingResponse)
async def start_meeting(
    meeting: MeetingCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a meeting and save it to PostgreSQL."""
    meeting_id = str(uuid.uuid4())
    started_at = datetime.now()

    db_meeting = Meeting(
        id=meeting_id,
        title=meeting.title,
        status="in_progress",
        scheduled_time=meeting.scheduled_time,
        started_at=started_at,
        owner_email=user.email,
        duration_minutes=meeting.duration_minutes,
        participants=meeting.participants,
    )

    try:
        db.add(db_meeting)
        db.commit()
        db.refresh(db_meeting)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to save meeting to database",
        )

    return MeetingResponse(
        id=db_meeting.id,
        title=db_meeting.title,
        status=db_meeting.status,
        started_at=db_meeting.started_at,
        ended_at=db_meeting.ended_at,
    )


RECORDING_SOURCES = {"upload", "browser_mic", "online_tab"}


@api.post("/api/v1/meetings/upload", status_code=201)
async def upload_meeting_audio(
    audio_file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=200),
    participants: str = Form(""),
    scheduled_time: datetime | None = Form(None),
    duration_minutes: Optional[int] = Form(None, ge=1, le=1440),
    timezone: Optional[str] = Form(None, max_length=64),
    # How the audio was made: a file ("upload"), the microphone ("browser_mic") or an online
    # meeting captured from a browser tab ("online_tab"), which needs the recorder's consent.
    source: Optional[str] = Form(None, max_length=20),
    recording_consent: bool = Form(False),
    # Put the meeting in one of your folders, or make a folder now by name: later meetings remember earlier ones.
    folder_id: Optional[str] = Form(None, max_length=64),
    new_folder: Optional[str] = Form(None, max_length=200),
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """Validate and store a recording, then create its meeting record."""
    folder = _pick_folder(db, user, folder_id, new_folder)
    if source is not None and source not in RECORDING_SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of: {', '.join(sorted(RECORDING_SOURCES))}")
    if source == "online_tab" and not recording_consent:
        raise HTTPException(
            status_code=422,
            detail="Confirm that everyone in the meeting knows it is being recorded.",
        )
    filename = audio_storage.sanitize_filename(audio_file.filename)
    try:
        ext = audio_storage.validate_extension(filename)
        meeting_id = str(uuid.uuid4())
        path, size = await audio_storage.save_upload(
            audio_file, meeting_id, ext
        )
    except audio_storage.AudioValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except OSError:
        logger.exception("Could not write uploaded audio")
        raise HTTPException(
            status_code=500, detail="Could not store the uploaded audio"
        )

    try:
        audio_seconds = audio_storage.probe_duration(path)
    except audio_storage.AudioValidationError as exc:
        audio_storage.remove_stored(path, meeting_id)
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    people = [p.strip() for p in participants.split(",") if p.strip()]
    if duration_minutes is None:
        # Not given: use the recording's real length (rounded up, at least 1).
        duration_minutes = max(1, math.ceil(audio_seconds / 60))
    meeting = Meeting(
        id=meeting_id,
        title=title.strip(),
        status="uploaded",
        scheduled_time=scheduled_time,
        duration_minutes=duration_minutes,
        timezone=_resolve_tz(timezone, user) if scheduled_time is not None else None,
        source=source,
        consent_at=datetime.utcnow() if (recording_consent and source == "online_tab") else None,
        participants=people,
        owner_email=user.email,
        folder_id=folder.id if folder else None,
        audio_file_path=path,
        audio_original_filename=filename,
        audio_size_bytes=size,
    )

    try:
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
    except Exception:
        db.rollback()
        audio_storage.remove_stored(path, meeting_id)
        logger.exception("Failed to save uploaded meeting")
        raise HTTPException(
            status_code=500, detail="Failed to save meeting to database"
        )

    return {
        "meeting_id": meeting.id,
        "title": meeting.title,
        "status": meeting.status,
        "participants": meeting.participants,
        "audio_filename": filename,
        "audio_size_bytes": size,
        "audio_duration_seconds": round(audio_seconds, 2),
        "created_at": meeting.created_at.isoformat(),
        "folder": {"id": folder.id, "name": folder.name} if folder else None,
    }


class MeetingScheduleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    participants: List[str] = Field(default_factory=list, max_length=100)
    scheduled_time: datetime
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    allow_conflicts: bool = False
    # The zone `scheduled_time` is in (e.g. "Asia/Kolkata"); defaults to the user's own zone.
    timezone: Optional[str] = Field(default=None, max_length=64)
    # Repeat the meeting: daily, weekdays, weekly, biweekly or monthly, for repeat_count occurrences or until a date.
    repeat: Optional[str] = Field(default=None, max_length=20)
    repeat_count: Optional[int] = Field(default=None, ge=2, le=recurrence.MAX_OCCURRENCES)
    repeat_until: Optional[date] = None
    folder_id: Optional[str] = Field(default=None, max_length=64)
    new_folder: Optional[str] = Field(default=None, max_length=200)


class MeetingUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    participants: Optional[List[str]] = Field(default=None, max_length=100)
    scheduled_time: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    allow_conflicts: bool = False
    timezone: Optional[str] = Field(default=None, max_length=64)


def _pick_folder(db: Session, user: User, folder_id: Optional[str], new_folder: Optional[str]) -> Optional[Folder]:
    """The folder a new meeting goes in: an existing one of the user's, or one made now by name."""
    try:
        if new_folder and new_folder.strip():
            return folders.get_or_create(db, user, new_folder)
        if folder_id and folder_id.strip():
            return folders.get_owned(db, user, folder_id.strip())
    except folders.FolderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return None


def _resolve_tz(requested: Optional[str], user: User) -> Optional[str]:
    """The zone for a new meeting: the one asked for, else the user's own, else none."""
    tz = (requested or "").strip() or None
    if tz and not timeutil.is_valid_tz(tz):
        raise HTTPException(status_code=422, detail="Unknown time zone. Use a name like Asia/Kolkata or Europe/London.")
    return tz or user.timezone


def _owner_names(db: Session, meetings: List[Meeting]) -> dict:
    """{owner e-mail: display name} for the given meetings."""
    emails = {m.owner_email for m in meetings if m.owner_email}
    if not emails:
        return {}
    return {e: n for e, n in db.query(User.email, User.name).filter(User.email.in_(emails)) if n}


def _meeting_to_dict(m: Meeting, user: Optional[User] = None,
                     stats: Optional[dict] = None, owner_names: Optional[dict] = None) -> dict:
    end = None
    if m.scheduled_time is not None:
        end = scheduling.meeting_end(m).isoformat()
    data = {
        "id": m.id,
        "title": m.title,
        "status": m.status,
        "scheduled_time": m.scheduled_time.isoformat() if m.scheduled_time else None,
        "end_time": end,
        "duration_minutes": m.duration_minutes,
        "timezone": m.timezone,
        "source": m.source,
        "consent_at": m.consent_at.isoformat() if m.consent_at else None,
        "series_id": m.series_id,
        "recurrence": m.recurrence,
        "participants": m.participants or [],
        "owner_email": m.owner_email,
        "started_at": m.started_at.isoformat() if m.started_at else None,
        "ended_at": m.ended_at.isoformat() if m.ended_at else None,
        "has_audio": bool(m.audio_file_path),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "folder": None,
    }
    if user is not None and m.folder is not None and auth.is_owner(user, m):
        data["folder"] = {"id": m.folder.id, "name": m.folder.name}      # folders are private to the owner
    if user is not None:
        data["can_write"] = auth.can_write(user, m)
        data["is_owner"] = auth.is_owner(user, m)
        data["owner_name"] = (owner_names or {}).get(m.owner_email)
    if stats is not None:
        data.update(stats.get(m.id, {"has_transcript": False, "has_summary": False,
                                     "action_count": 0, "open_action_count": 0}))
    return data


def _meeting_stats(db: Session, ids: List[str]) -> dict:
    """Per-meeting result counts, using a few grouped queries."""
    if not ids:
        return {}
    out = {i: {"has_transcript": False, "has_summary": False,
               "action_count": 0, "open_action_count": 0} for i in ids}
    for (mid,) in db.query(Transcript.meeting_id).filter(
            Transcript.meeting_id.in_(ids)).distinct():
        out[mid]["has_transcript"] = True
    for (mid,) in db.query(Summary.meeting_id).filter(
            Summary.meeting_id.in_(ids)).distinct():
        out[mid]["has_summary"] = True
    for mid, status, n in db.query(
            ActionItemDB.meeting_id, ActionItemDB.status, func.count()
    ).filter(ActionItemDB.meeting_id.in_(ids)).group_by(
            ActionItemDB.meeting_id, ActionItemDB.status):
        out[mid]["action_count"] += n
        if status != "completed":
            out[mid]["open_action_count"] += n
    return out


def _clean_people(people: List[str]) -> List[str]:
    seen, out = set(), []
    for p in people:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _conflict_view(m: Meeting, user: User) -> dict:
    """Full details only for meetings the user may read; others show busy time."""
    if auth.can_read(user, m):
        return _meeting_to_dict(m, user)
    return {
        "id": None,
        "title": "Busy",
        "scheduled_time": m.scheduled_time.isoformat() if m.scheduled_time else None,
        "end_time": scheduling.meeting_end(m).isoformat() if m.scheduled_time else None,
        "redacted": True,
    }


def _reject_conflicts(db, start, end, participants, exclude_id=None, user=None, tz=None):
    conflicts = scheduling.find_conflicts(
        db, start, end, participants, exclude_meeting_id=exclude_id, tz=tz
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The meeting conflicts with existing meetings. "
                "Choose another time (see /api/v1/calendar/find-optimal-time) "
                "or set allow_conflicts=true.",
                "conflicts": [_conflict_view(c, user) for c in conflicts],
            },
        )


def _check_not_past(when: datetime, tz: Optional[str] = None) -> None:
    # With a zone, compare real moments; without one, naive times mean the server's local time.
    past = (timeutil.to_utc(when, tz) < datetime.utcnow() - timedelta(minutes=5)) if tz else (
        when < datetime.now() - timedelta(minutes=5))
    if past:
        raise HTTPException(
            status_code=422, detail="scheduled_time must not be in the past"
        )


@api.post("/api/v1/meetings/schedule", status_code=201)
async def schedule_meeting(
    body: MeetingScheduleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Schedule a future meeting (or a repeating series), rejecting double-bookings."""
    folder = _pick_folder(db, user, body.folder_id, body.new_folder)
    tz = _resolve_tz(body.timezone, user)
    start = scheduling.to_naive(body.scheduled_time)
    _check_not_past(start, tz)
    people = _clean_people(body.participants)
    dates = [start]
    if body.repeat:
        try:
            dates = recurrence.occurrences(start, body.repeat, body.repeat_count, body.repeat_until)
        except recurrence.RecurrenceError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    elif body.repeat_count or body.repeat_until:
        raise HTTPException(status_code=422, detail="Choose how often to repeat (repeat), or remove the end of the repeat")
    series_id = str(uuid.uuid4()) if len(dates) > 1 else None

    created, skipped = [], []
    try:
        scheduling.lock_calendar(db)
        for i, when in enumerate(dates):
            end = when + timedelta(minutes=body.duration_minutes)
            if not body.allow_conflicts:
                if i == 0:
                    _reject_conflicts(db, when, end, people + [user.email], user=user, tz=tz)
                else:
                    clash = scheduling.find_conflicts(db, when, end, people + [user.email], tz=tz)
                    if clash:
                        skipped.append({"scheduled_time": when.isoformat(),
                                        "reason": f"Conflicts with “{clash[0].title}”"})
                        continue
            meeting = Meeting(
                id=str(uuid.uuid4()),
                title=body.title.strip(),
                status="scheduled",
                owner_email=user.email,
                scheduled_time=when,
                duration_minutes=body.duration_minutes,
                participants=people,
                timezone=tz,
                series_id=series_id,
                recurrence=body.repeat if series_id else None,
                folder_id=folder.id if folder else None,
            )
            db.add(meeting)
            created.append(meeting)
        db.commit()
        for m in created:
            db.refresh(m)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to schedule meeting")
        raise HTTPException(status_code=500, detail="Failed to schedule meeting")
    notifications.notify_shared(db, created[0], scheduling.norm_people(people), user)
    out = _meeting_to_dict(created[0], user)
    if series_id:
        out["series"] = {"id": series_id, "recurrence": body.repeat, "created": len(created), "skipped": skipped}
    return out


@api.get("/api/v1/meetings")
async def list_meetings(
    status: Optional[str] = None,
    participant: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    folder_id: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List stored meetings, newest first, with optional filters."""
    try:
        start = internal_calendar.parse_bound(start_date, end=False)
        end = internal_calendar.parse_bound(end_date, end=True)
    except internal_calendar.CalendarFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    when = func.coalesce(Meeting.scheduled_time, Meeting.started_at, Meeting.created_at)
    query = db.query(Meeting)
    if status:
        query = query.filter(
            func.lower(Meeting.status).in_(
                [x.strip().lower() for x in status.split(",") if x.strip()]
            )
        )
    if start:
        query = query.filter(when >= start)
    if end:
        query = query.filter(when <= end)
    if folder_id:
        folder_id = _folder_or_http(db, user, folder_id).id     # your own folders only
        query = query.filter(Meeting.folder_id == folder_id)

    meetings = query.order_by(when.desc(), Meeting.id).all()
    meetings = [m for m in meetings if auth.can_read(user, m)]
    if participant:
        who = participant.strip().lower()
        meetings = [m for m in meetings if who in scheduling.norm_people(m.participants)]

    page = meetings[offset:offset + limit]
    stats = _meeting_stats(db, [m.id for m in page])
    return {
        "count": len(page),
        "total": len(meetings),
        "meetings": [_meeting_to_dict(m, user, stats, _owner_names(db, page)) for m in page],
    }


@api.get("/api/v1/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    data = _meeting_to_dict(meeting, user, _meeting_stats(db, [meeting.id]),
                            _owner_names(db, [meeting]))
    if data.get("folder"):
        data["folder"]["number"] = folders.number_of(db, meeting)        # "Meeting 2" in its folder
        data["folder"]["meeting_count"] = len(folders.ordered(db, meeting.folder_id))
    if meeting.series_id:
        siblings = (db.query(Meeting.id).filter(Meeting.series_id == meeting.series_id)
                    .order_by(Meeting.scheduled_time, Meeting.id).all())
        ids = [r[0] for r in siblings]
        data["series"] = {"count": len(ids), "index": ids.index(meeting.id) + 1 if meeting.id in ids else None}
    data["audio_seconds"] = None
    if meeting.audio_file_path and os.path.exists(meeting.audio_file_path):
        try:
            data["audio_seconds"] = round(
                await run_in_threadpool(audio_storage.probe_duration, meeting.audio_file_path), 1
            )
        except audio_storage.AudioValidationError:
            pass                      # the length is a nicety; never fail the page
    return data


@api.put("/api/v1/meetings/{meeting_id}")
async def update_meeting(
    meeting_id: str,
    body: MeetingUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reschedule or edit a scheduled meeting (re-checks conflicts)."""
    try:
        scheduling.lock_calendar(db)
        meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
        if meeting.status != "scheduled":
            raise HTTPException(
                status_code=409,
                detail=f"Only scheduled meetings can be edited "
                f"(this one is {meeting.status})",
            )

        title = body.title.strip() if body.title else meeting.title
        people = (
            _clean_people(body.participants)
            if body.participants is not None
            else (meeting.participants or [])
        )
        start = scheduling.to_naive(body.scheduled_time) or meeting.scheduled_time
        minutes = body.duration_minutes or meeting.duration_minutes or 60
        end = start + timedelta(minutes=minutes)
        tz = _resolve_tz(body.timezone, user) if body.timezone is not None else meeting.timezone

        time_changed = (
            start != meeting.scheduled_time
            or minutes != (meeting.duration_minutes or 60)
            or body.participants is not None
            or tz != meeting.timezone
        )
        if body.scheduled_time is not None or tz != meeting.timezone:
            _check_not_past(start, tz)
        if time_changed and not body.allow_conflicts:
            _reject_conflicts(
                db, start, end,
                people + ([meeting.owner_email] if meeting.owner_email else []),
                exclude_id=meeting_id, user=user, tz=tz,
            )
        meeting.timezone = tz

        newly_invited = scheduling.norm_people(people) - scheduling.norm_people(meeting.participants)
        meeting.title = title
        meeting.participants = people
        meeting.scheduled_time = start
        meeting.duration_minutes = minutes
        db.commit()
        db.refresh(meeting)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to update meeting")
        raise HTTPException(status_code=500, detail="Failed to update meeting")
    notifications.notify_shared(db, meeting, newly_invited, user)
    return _meeting_to_dict(meeting, user)


@api.post("/api/v1/meetings/{meeting_id}/cancel")
async def cancel_meeting(
    meeting_id: str,
    scope: str = Query(default="this", pattern="^(this|following)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancel a scheduled meeting. The record is kept, with status cancelled. For a repeating
    meeting, scope=following also cancels every later meeting in the series.
    """
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    if meeting.status == "cancelled":
        return {**_meeting_to_dict(meeting, user), "already_cancelled": True}
    if meeting.status != "scheduled":
        raise HTTPException(
            status_code=409,
            detail=f"Only scheduled meetings can be cancelled "
            f"(this one is {meeting.status})",
        )
    meeting.status = "cancelled"
    cancelled = 1
    if scope == "following" and meeting.series_id:
        later = (db.query(Meeting).filter(Meeting.series_id == meeting.series_id, Meeting.id != meeting.id,
                                          Meeting.status == "scheduled",
                                          Meeting.scheduled_time > meeting.scheduled_time).all())
        for m in later:
            if auth.can_write(user, m):
                m.status = "cancelled"
                cancelled += 1
    try:
        db.commit()
        db.refresh(meeting)
    except Exception:
        db.rollback()
        logger.exception("Failed to cancel meeting")
        raise HTTPException(status_code=500, detail="Failed to cancel meeting")
    out = _meeting_to_dict(meeting, user)
    if scope == "following":
        out["cancelled_count"] = cancelled
    return out


@api.get("/api/v1/meetings/{meeting_id}/series")
async def meeting_series(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every meeting of a repeating series you can read, in date order."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    if not meeting.series_id:
        raise HTTPException(status_code=404, detail="This meeting does not repeat")
    rows = (db.query(Meeting).filter(Meeting.series_id == meeting.series_id)
            .order_by(Meeting.scheduled_time, Meeting.id).all())
    return {
        "series_id": meeting.series_id,
        "recurrence": meeting.recurrence,
        "label": recurrence.LABEL.get(meeting.recurrence or "", ""),
        "meetings": [{"id": m.id, "scheduled_time": m.scheduled_time.isoformat() if m.scheduled_time else None,
                      "status": m.status, "is_current": m.id == meeting.id}
                     for m in rows if auth.can_read(user, m)],
    }


@api.post("/api/v1/meetings/{meeting_id}/end")
async def end_meeting(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """End a meeting and save its completion status."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)

    meeting.status = "completed"
    meeting.ended_at = datetime.now()

    try:
        db.commit()
        db.refresh(meeting)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to update meeting in database",
        )

    return {
        "meeting_id": meeting.id,
        "status": meeting.status,
        "ended_at": meeting.ended_at.isoformat(),
    }


# Transcription endpoints

@api.post("/api/v1/meetings/{meeting_id}/transcribe")
async def transcribe_meeting(
    meeting_id: str,
    audio_file: UploadFile | None = File(default=None),
    language: str = Query(default="en", pattern=r"^(auto|[a-z]{2,3})$"),
    identify_speakers: bool = False,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    Transcribe the meeting's stored audio (or a newly attached file) now.

    For long recordings use POST /api/v1/meetings/{id}/process, which runs
    in the background.
    """
    safe_filename = os.path.basename(
        (audio_file.filename if audio_file else None) or "audio"
    )
    temp_path = f"/tmp/{uuid.uuid4()}_{safe_filename}"
    audio_path = temp_path

    try:
        meeting = _get_meeting_or_404(db, meeting_id, user, write=True)

        if audio_file is not None:
            content = await audio_file.read()
            if not content:
                raise HTTPException(
                    status_code=400, detail="Uploaded audio file is empty"
                )
            with open(temp_path, "wb") as audio:
                audio.write(content)
        elif meeting.audio_file_path and os.path.exists(meeting.audio_file_path):
            audio_path = meeting.audio_file_path
        else:
            raise HTTPException(
                status_code=400,
                detail="No audio available. Upload one via "
                "/api/v1/meetings/upload or attach audio_file.",
            )

        # CPU-bound: keep it off the event loop so other requests are served.
        transcript, result = await run_in_threadpool(
            pipeline.transcribe_step,
            db, meeting, audio_path,
            language=language,
            identify_speakers=identify_speakers,
            store_path=audio_path != temp_path,
            transcriber=transcriber,
        )

        return {
            "meeting_id": meeting_id,
            "transcript_id": transcript.id,
            "word_count": transcript.word_count,
            "duration_seconds": result.duration_seconds,
            "processing_seconds": result.processing_seconds,
            "speaker_count": transcript.speaker_count,
            "segments": transcript.segments,
            "saved_to_database": True,
        }

    except HTTPException:
        raise
    except pipeline.PipelineError as exc:
        db.rollback()
        logger.error("Transcription failed: %s", exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        db.rollback()
        logger.exception("Transcription failed")
        raise HTTPException(
            status_code=500,
            detail="Transcription or database save failed",
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


class ProcessRequest(BaseModel):
    steps: List[str] = Field(default_factory=lambda: list(pipeline.STEPS))
    language: str = Field(default="en", pattern=r"^(auto|[a-z]{2,3})$")
    identify_speakers: bool = True
    regenerate_summary: bool = False


_AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac",
                ".webm": "audio/webm"}


@api.get("/api/v1/meetings/{meeting_id}/audio")
async def get_meeting_audio(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download the stored recording (for playback)."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    path = meeting.audio_file_path
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No audio stored for this meeting")
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(
        path,
        media_type=_AUDIO_TYPES.get(ext, "application/octet-stream"),
        filename=meeting.audio_original_filename or os.path.basename(path),
        content_disposition_type="inline",
    )


@api.delete("/api/v1/meetings/{meeting_id}", status_code=204)
async def delete_meeting(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Permanently delete a meeting with its transcript, summaries, action items
    and stored audio. Owner (or admin) only; refused while it is processing.
    """
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    try:
        active = job_store.active_job_for(meeting_id)
    except Exception:
        active = None
    if active is not None and active.get("state") in ("queued", "running", "retrying"):
        raise HTTPException(
            status_code=409,
            detail="This meeting is being processed. Try again when it finishes.",
        )
    audio_path = meeting.audio_file_path
    try:
        task_ids = [r[0] for r in db.query(ActionItemDB.id).filter(ActionItemDB.meeting_id == meeting_id)]
        if task_ids:
            db.query(TaskComment).filter(TaskComment.action_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(Notification).filter(Notification.meeting_id == meeting_id).delete()
        db.query(ActionItemDB).filter(ActionItemDB.meeting_id == meeting_id).delete()
        db.query(AskHistory).filter(AskHistory.meeting_id == meeting_id).delete()
        db.query(Summary).filter(Summary.meeting_id == meeting_id).delete()
        db.query(Transcript).filter(Transcript.meeting_id == meeting_id).delete()
        db.delete(meeting)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to delete meeting")
        raise HTTPException(status_code=500, detail="Failed to delete meeting")
    audio_storage.remove_stored(audio_path, meeting_id)
    return Response(status_code=204)


@api.get("/api/v1/actions")
async def list_all_actions(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    meeting_id: Optional[str] = None,
    due_from: Optional[str] = None,
    due_to: Optional[str] = None,
    has_due_date: Optional[bool] = None,
    needs_review: Optional[bool] = None,
    mine: Optional[bool] = None,
    sort: str = Query(default="due", pattern="^(due|created)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Action items across every meeting you can access (the "my tasks" view).
    Filters: status (comma-separated), assignee (substring), meeting_id,
    due_from/due_to (YYYY-MM-DD, inclusive), has_due_date.
    """
    try:
        lo = internal_calendar.parse_bound(due_from, end=False)
        hi = internal_calendar.parse_bound(due_to, end=True)
    except internal_calendar.CalendarFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    wanted = internal_calendar.parse_csv(status)
    if wanted and not wanted <= {"pending", "in_progress", "completed"}:
        raise HTTPException(status_code=422, detail="Unknown status value")

    accessible = {m.id: m for m in db.query(Meeting).all() if auth.can_read(user, m)}
    if meeting_id:
        accessible = {k: v for k, v in accessible.items() if k == meeting_id}
    if not accessible:
        return {"count": 0, "total": 0, "action_items": []}

    query = db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(list(accessible)))
    if wanted:
        query = query.filter(func.lower(ActionItemDB.status).in_(wanted))
    if assignee:
        query = query.filter(func.lower(ActionItemDB.assignee).contains(assignee.strip().lower()))
    if lo:
        query = query.filter(ActionItemDB.due_date >= lo)
    if hi:
        query = query.filter(ActionItemDB.due_date <= hi)
    if needs_review is True:
        query = query.filter(ActionItemDB.due_date.is_(None), ActionItemDB.due_phrase.isnot(None),
                             ActionItemDB.status != "completed")
    if has_due_date is True:
        query = query.filter(ActionItemDB.due_date.isnot(None))
    elif has_due_date is False:
        query = query.filter(ActionItemDB.due_date.is_(None))

    if sort == "created":
        query = query.order_by(ActionItemDB.created_at.desc(), ActionItemDB.id)
    else:   # soonest deadline first; undated last
        query = query.order_by(ActionItemDB.due_date.is_(None),
                               ActionItemDB.due_date, ActionItemDB.id)
    ctx = assignments.Context(db, user)
    if mine is None:
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
    else:
        # Ownership is decided in Python (names), so filter before paging.
        candidates = query.all()
        ctx.preload(candidates)
        every = [r for r in candidates
                 if ctx.view(accessible[r.meeting_id], r)["mine"] == mine]
        total = len(every)
        rows = every[offset:offset + limit]
    ctx.preload(rows)
    return {
        "count": len(rows),
        "total": total,
        "action_items": [
            {**_action_to_dict(r),
             **ctx.view(accessible[r.meeting_id], r),
             "meeting_id": r.meeting_id,
             "meeting_title": accessible[r.meeting_id].title,
             "can_write": auth.can_write(user, accessible[r.meeting_id])}
            for r in rows
        ],
    }


@api.post("/api/v1/meetings/{meeting_id}/process", status_code=202)
async def process_meeting_async(
    meeting_id: str,
    body: Optional[ProcessRequest] = None,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    Process a meeting in the background (Celery): transcribe, summarize and
    extract action items. Poll GET /api/v1/jobs/{job_id} for progress.
    """
    from ..workers.tasks import task_for  # imported lazily: builds Celery app

    body = body or ProcessRequest()
    unknown = set(body.steps) - set(pipeline.STEPS)
    if unknown or not body.steps:
        raise HTTPException(
            status_code=422,
            detail=f"steps must be a non-empty subset of {list(pipeline.STEPS)}",
        )
    steps = [s for s in pipeline.STEPS if s in body.steps]  # canonical order

    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    if "transcribe" in steps:
        if not meeting.audio_file_path or not os.path.exists(meeting.audio_file_path):
            raise HTTPException(
                status_code=400,
                detail="No stored audio. Upload a recording via "
                "/api/v1/meetings/upload first.",
            )
    elif pipeline.latest_transcript(db, meeting_id) is None:
        raise HTTPException(
            status_code=400,
            detail="No transcript yet. Include the 'transcribe' step.",
        )

    options = {
        "language": body.language,
        "identify_speakers": body.identify_speakers,
        "regenerate_summary": body.regenerate_summary,
    }
    return _enqueue_job(meeting_id, steps, options)


def _enqueue_job(meeting_id: str, steps: List[str], options: dict) -> dict:
    """Lock the meeting and queue a background job for the given steps."""
    from ..workers.tasks import task_for  # imported lazily: builds Celery app

    job_id = str(uuid.uuid4())

    try:
        holder = job_store.acquire_lock(meeting_id, job_id)
        if holder is not None and job_store.reclaim_if_stale(meeting_id):
            holder = job_store.acquire_lock(meeting_id, job_id)
        if holder is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This meeting is already being processed.",
                    "job_id": holder,
                    "status_url": f"/api/v1/jobs/{holder}",
                },
            )
        job_store.create_job(meeting_id, steps, options, job_id=job_id)
        try:
            task_for(steps).apply_async(
                args=[job_id, meeting_id, options], task_id=job_id
            )
        except Exception:
            job_store.release_lock(meeting_id, job_id)
            raise
    except HTTPException:
        raise
    except Exception:
        logger.exception("Could not queue processing job")
        raise HTTPException(
            status_code=503,
            detail="Background processing is unavailable (message broker "
            "unreachable). Try again shortly.",
        )

    return {
        "job_id": job_id,
        "meeting_id": meeting_id,
        "state": "queued",
        "steps": steps,
        "status_url": f"/api/v1/jobs/{job_id}",
    }


def _job_view(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "meeting_id": job["meeting_id"],
        "state": job["state"],
        "progress_percent": job["progress_percent"],
        "attempts": job["attempts"],
        "error": job["error"],
        "steps": job["steps"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@api.get("/api/v1/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Status and progress of a background processing job."""
    try:
        job = job_store.get_job(job_id)
    except Exception:
        logger.exception("Job store unavailable")
        raise HTTPException(status_code=503, detail="Job store unavailable")
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    _get_meeting_or_404(db, job["meeting_id"], user)   # same access as the meeting
    return _job_view(job)


@api.get("/api/v1/meetings/{meeting_id}/jobs/latest")
async def get_latest_job(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The most recent processing job for a meeting."""
    _get_meeting_or_404(db, meeting_id, user, write=False)
    try:
        job_id = job_store.latest_job_id(meeting_id)
        job = job_store.get_job(job_id) if job_id else None
    except Exception:
        logger.exception("Job store unavailable")
        raise HTTPException(status_code=503, detail="Job store unavailable")
    if job is None:
        raise HTTPException(status_code=404, detail="No processing job found")
    return _job_view(job)


class SpeakerRename(BaseModel):
    label: str = Field(max_length=200)   # current label, e.g. "Speaker 1"
    name: str = Field(max_length=200)
    remember_voice: bool = True
    # Give this person access to the meeting so they can sign in and see their tasks.
    email: Optional[str] = Field(default=None, max_length=254)
    # The mis-heard spelling in the transcript ("Megapanvala"): fixed everywhere to the chosen name.
    replace_heard: Optional[str] = Field(default=None, max_length=200)
    # Correcting a wrong match: also remove the previously named person's access to this meeting.
    revoke_previous: bool = False


@api.get("/api/v1/people")
async def list_people(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Members that can be picked when naming a speaker (their name and sign-in e-mail)."""
    people = voices.people_directory(db)
    return {"count": len(people), "people": people}


def _known_names(db: Session, meeting: Meeting, user: User) -> List[str]:
    """Names of people this meeting already knows: saved voices, the owner, and participants."""
    owner = voices.owner_user(db, meeting) or user
    names = {p.display_name for p in voices.profiles_for(db, owner.id)}
    names |= {p["name"] for p in voices.people_directory(db)}
    emails = {str(p).strip().lower() for p in (meeting.participants or [])} | {owner.email.lower()}
    for (name,) in db.query(User.name).filter(User.email.in_(list(emails))):
        if name:
            names.add(name)
    return sorted(names)


@api.get("/api/v1/meetings/{meeting_id}/speakers")
async def list_meeting_speakers(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Speakers in the latest transcript, and whether their voice is saved."""
    _get_meeting_or_404(db, meeting_id, user, write=False)
    transcript = voices.latest_transcript(db, meeting_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="No transcript found for this meeting")
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    speakers = voices.list_speakers(transcript, _known_names(db, meeting, user),
                                    voices.candidate_profiles(db, meeting))
    for s in speakers:                       # who a named speaker is, so a wrong match can be corrected
        s["email"] = voices.email_for_name(db, s["label"]) if s["named"] else None
    return {
        "meeting_id": meeting_id,
        "speakers": speakers,
        "voices_available": bool(transcript.speaker_voices),
    }


@api.put("/api/v1/meetings/{meeting_id}/speakers")
async def rename_meeting_speaker(
    meeting_id: str,
    body: SpeakerRename,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Name a speaker (e.g. "Speaker 1" -> "Krishna Panwala"). The name replaces
    the label in the transcript, summary and action items, and the voice is
    remembered so it is recognised automatically in later meetings.
    """
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    email = auth.validate_email(body.email) if body.email and body.email.strip() else None
    try:
        out = voices.rename_speaker(
            db, meeting, body.label, body.name, user,
            remember_voice=body.remember_voice, email=email, revoke_previous=body.revoke_previous,
        )
        heard = " ".join((body.replace_heard or "").split())
        if heard and heard.lower() != out["label"].lower():
            out["text_fixed"] = transcript_edit.replace_everywhere(
                db, meeting, heard, out["label"], whole_word=True, include_summary=True
            )["total"]
        return out
    except voices.VoiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@api.post("/api/v1/meetings/{meeting_id}/speakers/analyze", status_code=202)
async def analyze_meeting_voices(
    meeting_id: str,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    Extract voice fingerprints for a meeting that was processed before voices
    were recorded, so its speakers can be remembered. Text is not changed.
    """
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    if pipeline.latest_transcript(db, meeting_id) is None:
        raise HTTPException(status_code=400, detail="No transcript yet.")
    if not meeting.audio_file_path or not os.path.exists(meeting.audio_file_path):
        raise HTTPException(status_code=400, detail="No stored audio for this meeting.")
    return _enqueue_job(meeting_id, ["voices"], {})


@api.get("/api/v1/voice-profiles")
async def list_voice_profiles(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The people whose voices the signed-in user has named."""
    rows = (db.query(VoiceProfile).filter(VoiceProfile.owner_id == user.id)
            .order_by(VoiceProfile.display_name).all())
    return {
        "count": len(rows),
        "profiles": [
            {"id": r.id, "name": r.display_name, "samples": len(r.embeddings or []),
             "email": r.email,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ],
    }


class VoiceProfileUpdate(BaseModel):
    email: Optional[str] = Field(default=None, max_length=254)   # blank or null removes it


@api.patch("/api/v1/voice-profiles/{profile_id}")
async def update_voice_profile(
    profile_id: str,
    body: VoiceProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set or clear the e-mail meetings are shared with when this voice is recognised."""
    row = (db.query(VoiceProfile)
           .filter(VoiceProfile.id == profile_id, VoiceProfile.owner_id == user.id).first())
    if row is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    row.email = auth.validate_email(body.email) if body.email and body.email.strip() else None
    db.commit()
    return {"id": row.id, "name": row.display_name, "email": row.email,
            "samples": len(row.embeddings or [])}


MY_VOICE_MAX_BYTES = 20 * 1024 * 1024
MY_VOICE_SECONDS = (2.0, 120.0)


@api.post("/api/v1/voice-profiles/me", status_code=201)
async def record_my_voice(
    audio_file: UploadFile = File(...),
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    Save the voice in a short recording of yourself (a few seconds to two minutes, alone), so you
    are named automatically in meetings. It is stored under your own name and e-mail.
    """
    if not (user.name or "").strip():
        raise HTTPException(status_code=422, detail="Add your full name first (Settings), so your voice can be named.")
    meeting_id = f"voice-{user.id}"
    path = None
    try:
        ext = audio_storage.validate_extension(audio_storage.sanitize_filename(audio_file.filename))
        path, _size = await audio_storage.save_upload(audio_file, meeting_id, ext, limit=MY_VOICE_MAX_BYTES)
        seconds = audio_storage.probe_duration(path)
        low, high = MY_VOICE_SECONDS
        if seconds < low:
            raise HTTPException(status_code=422, detail="That recording is too short. Speak for at least 4 seconds.")
        if seconds > high:
            raise HTTPException(status_code=422, detail="That recording is too long. Keep it under two minutes.")
        try:
            found = await run_in_threadpool(transcriber.extract_single_voice, path)
        except NoSpeechError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except DiarizationUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        profile = voices.enroll(db, user.id, user.name.strip(), found["embedding"], email=user.email)
        db.commit()
        db.refresh(profile)
    except audio_storage.AudioValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    finally:
        if path:
            audio_storage.remove_stored(path, meeting_id)
    return {"id": profile.id, "name": profile.display_name, "email": profile.email,
            "samples": len(profile.embeddings or []), "speech_seconds": found["speech_seconds"]}


@api.delete("/api/v1/voice-profiles/{profile_id}", status_code=204)
async def delete_voice_profile(
    profile_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Forget a saved voice (the names already written into meetings stay)."""
    row = (db.query(VoiceProfile)
           .filter(VoiceProfile.id == profile_id, VoiceProfile.owner_id == user.id).first())
    if row is None:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


class SegmentEdit(BaseModel):
    text: str = Field(max_length=transcript_edit.MAX_SEGMENT_CHARS * 2)


class ReplaceText(BaseModel):
    find: str = Field(max_length=transcript_edit.MAX_FIND_CHARS * 2)
    replace: str = Field(max_length=transcript_edit.MAX_FIND_CHARS * 2)
    match_case: bool = False
    whole_word: bool = True
    include_summary: bool = True


@api.patch("/api/v1/meetings/{meeting_id}/transcript/segments/{segment_id}")
async def edit_transcript_segment(
    meeting_id: str,
    segment_id: int,
    body: SegmentEdit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Correct the words of one transcript segment (owner only)."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    try:
        return transcript_edit.edit_segment(db, meeting, segment_id, body.text)
    except voices.VoiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@api.post("/api/v1/meetings/{meeting_id}/transcript/replace")
async def replace_transcript_text(
    meeting_id: str,
    body: ReplaceText,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace a mis-heard word everywhere: transcript, and optionally summary and tasks."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    try:
        return transcript_edit.replace_everywhere(
            db, meeting, body.find, body.replace,
            match_case=body.match_case, whole_word=body.whole_word,
            include_summary=body.include_summary,
        )
    except voices.VoiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@api.get("/api/v1/search")
async def search_everything(
    q: str = Query(min_length=2, max_length=search_service.MAX_QUERY),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Find words in the titles, summaries, tasks and transcripts of meetings you can read."""
    return search_service.search(db, user, q)


@api.get("/api/v1/analytics")
async def meeting_analytics(
    days: int = Query(default=90, ge=analytics.MIN_DAYS, le=analytics.MAX_DAYS),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Talk time, task completion and weekly trends over the meetings you can read."""
    return analytics.compute(db, user, days)


@api.get("/api/v1/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=30, ge=1, le=notifications.MAX_LIST),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Your notifications, newest first (reminders are brought up to date first)."""
    reminders.maybe_scan_for(db, user)
    return {"unread": notifications.unread_count(db, user),
            "items": [notifications.view(n) for n in notifications.listing(db, user, unread_only, limit)]}


class MarkRead(BaseModel):
    ids: Optional[List[str]] = Field(default=None, max_length=200)   # omit to mark everything read


@api.post("/api/v1/notifications/read")
async def mark_notifications_read(
    body: MarkRead,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    n = notifications.mark_read(db, user, body.ids)
    return {"marked": n, "unread": notifications.unread_count(db, user)}


@api.delete("/api/v1/notifications/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
           if _safe_id(notification_id) else None)
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@api.get("/api/v1/features")
async def features(user: User = Depends(get_current_user)):
    """What this installation can do (e.g. whether the server can send e-mail)."""
    return {"email_enabled": mailer.email_enabled()}


def _ics_response(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@api.get("/api/v1/export/calendar.ics")
async def export_calendar(
    meetings: bool = True,
    tasks: bool = True,
    mine: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Scheduled meetings and open task deadlines you can read, as an iCalendar file to
    import into Google, Apple or Outlook calendars. Tasks are your own unless mine=false.
    """
    accessible = {m.id: m for m in db.query(Meeting).all() if auth.can_read(user, m)}
    now = datetime.utcnow()
    events = []
    if meetings:
        for m in sorted(accessible.values(), key=lambda x: x.scheduled_time or datetime.min):
            if m.scheduled_time is not None and m.status != "cancelled":
                events.append(ical.meeting_event(m, now, summary_email.app_link(m.id)))
    if tasks and accessible:
        ctx = assignments.Context(db, user)
        rows = (db.query(ActionItemDB)
                .filter(ActionItemDB.meeting_id.in_(list(accessible)), ActionItemDB.status != "completed",
                        ActionItemDB.due_date.isnot(None))
                .order_by(ActionItemDB.due_date, ActionItemDB.id).all())
        for a in rows:
            if not mine or ctx.view(accessible[a.meeting_id], a)["mine"]:
                events.append(ical.task_event(a, accessible[a.meeting_id].title, now))
    return _ics_response(ical.build_calendar(events), "meeting-intelligence.ics")


@api.get("/api/v1/meetings/{meeting_id}/calendar.ics")
async def export_meeting_calendar(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One meeting, plus its open tasks that have a deadline, as an iCalendar file."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    now = datetime.utcnow()
    events = [ical.meeting_event(meeting, now, summary_email.app_link(meeting.id))]
    for a in (db.query(ActionItemDB)
              .filter(ActionItemDB.meeting_id == meeting_id, ActionItemDB.status != "completed",
                      ActionItemDB.due_date.isnot(None)).order_by(ActionItemDB.due_date, ActionItemDB.id)):
        events.append(ical.task_event(a, meeting.title, now))
    return _ics_response(ical.build_calendar(events, meeting.title), f"{(re.sub(r'[^A-Za-z0-9_-]+', '-', meeting.title).strip('-') or 'meeting')[:60]}.ics")


class SendSummary(BaseModel):
    recipients: Optional[List[str]] = Field(default=None, max_length=summary_email.MAX_RECIPIENTS)
    note: str = Field(default="", max_length=summary_email.MAX_NOTE)


@api.post("/api/v1/meetings/{meeting_id}/send-summary")
async def send_summary(
    meeting_id: str,
    body: SendSummary,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    E-mail the summary and each person's own tasks to people who already have access to
    the meeting (its participants, or the owner). Owner only.
    """
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    if not mailer.email_enabled():
        raise HTTPException(
            status_code=503,
            detail="E-mail sending is not set up on this server. Set EMAIL_ENABLED=true and the "
                   "SMTP_* settings in .env, or use “Open in mail app” instead.",
        )
    summary = (db.query(Summary).filter(Summary.meeting_id == meeting_id)
               .order_by(Summary.created_at.desc()).first())
    if summary is None:
        raise HTTPException(status_code=409, detail="This meeting has no summary yet. Process it first.")

    allowed = {str(p).strip().lower() for p in (meeting.participants or []) if "@" in str(p)}
    if meeting.owner_email:
        allowed.add(meeting.owner_email.lower())
    wanted = body.recipients if body.recipients is not None else sorted(
        e for e in allowed if e != (meeting.owner_email or "").lower())
    recipients, seen = [], set()
    for raw in wanted:
        email = auth.validate_email(raw)
        if email not in allowed:
            raise HTTPException(status_code=422, detail=f"{email} is not a participant of this meeting")
        if email not in seen:
            seen.add(email)
            recipients.append(email)
    if not recipients:
        raise HTTPException(status_code=422, detail="Choose at least one recipient. Add participants to the meeting first.")

    actions = (db.query(ActionItemDB).filter(ActionItemDB.meeting_id == meeting_id)
               .order_by(ActionItemDB.due_date.is_(None), ActionItemDB.due_date, ActionItemDB.id).all())
    sent, failed = [], []
    for email in recipients:
        person = db.query(User).filter(User.email == email).first()
        subject, text = summary_email.compose(meeting, summary, actions, email, person,
                                              user.name or user.email, body.note)
        ok = await run_in_threadpool(mailer.send_email, email, subject, text)
        (sent if ok else failed).append(email)
    if not sent:
        raise HTTPException(status_code=502, detail="The e-mail server did not accept the messages. Check the SMTP settings.")
    return {"sent": sent, "failed": failed}


class AskTurn(BaseModel):
    question: str = Field(max_length=500)
    answer: str = Field(max_length=2000)


# ---------------------------------------------------------------- folders

class FolderBody(BaseModel):
    name: str = Field(min_length=1, max_length=folders.MAX_NAME_LENGTH * 2)


class MoveToFolder(BaseModel):
    folder_id: Optional[str] = Field(default=None, max_length=64)    # null: take it out of its folder
    new_folder: Optional[str] = Field(default=None, max_length=200)  # or put it in a new folder, made now


def _folder_view(db: Session, f: Folder) -> dict:
    members = folders.ordered(db, f.id)
    latest = max((folders.when(m) for m in members), default=None)
    return {"id": f.id, "name": f.name, "meeting_count": len(members),
            "last_meeting": latest.isoformat() if latest else None,
            "created_at": f.created_at.isoformat() if f.created_at else None}


def _folder_or_http(db: Session, user: User, folder_id: str) -> Folder:
    try:
        return folders.get_owned(db, user, folder_id)
    except folders.FolderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@api.get("/api/v1/folders")
async def list_folders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The signed-in user's folders (private to them), with how many meetings each holds."""
    rows = db.query(Folder).filter(Folder.owner_id == user.id).order_by(Folder.name_key).all()
    return {"count": len(rows), "folders": [_folder_view(db, f) for f in rows]}


@api.post("/api/v1/folders", status_code=201)
async def create_folder(body: FolderBody, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        f = folders.create(db, user, body.name)
        db.commit()
    except folders.FolderError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return _folder_view(db, f)


@api.get("/api/v1/folders/{folder_id}")
async def get_folder(folder_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """A folder with its meetings in order (Meeting 1, 2, 3...), each with a short summary."""
    f = _folder_or_http(db, user, folder_id)
    return {**_folder_view(db, f), "meetings": folders.timeline(db, f)}


@api.patch("/api/v1/folders/{folder_id}")
async def rename_folder(folder_id: str, body: FolderBody, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    f = _folder_or_http(db, user, folder_id)
    try:
        folders.rename(db, user, f, body.name)
        db.commit()
    except folders.FolderError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return _folder_view(db, f)


@api.delete("/api/v1/folders/{folder_id}")
async def delete_folder(folder_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete the folder only: its meetings, summaries and tasks are kept and become unfiled."""
    f = _folder_or_http(db, user, folder_id)
    freed = folders.delete(db, f)
    db.commit()
    return {"deleted": True, "meetings_unfiled": freed}


@api.put("/api/v1/meetings/{meeting_id}/folder")
async def move_meeting_to_folder(
    meeting_id: str,
    body: MoveToFolder,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Put one of your meetings in a folder (an existing one or a new one), or take it out (folder_id null)."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    target = _pick_folder(db, user, body.folder_id, body.new_folder)
    try:
        folders.assign(db, user, meeting, target.id if target else None)
        db.commit()
    except folders.FolderError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    db.refresh(meeting)
    return _meeting_to_dict(meeting, user)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    meeting_id: Optional[str] = Field(default=None, max_length=64)   # limit the search to one meeting
    folder_id: Optional[str] = Field(default=None, max_length=64)    # ... or to the meetings in one folder
    history: List[AskTurn] = Field(default_factory=list, max_length=6)


@api.post("/api/v1/ask")
async def ask_your_meetings(
    body: AskRequest,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """
    Answer a question from the meetings the signed-in user can read (or one meeting),
    using the local Ollama model. The answer cites its sources as [M1], [M2]...
    """
    question = " ".join(body.question.split())
    if len(question) < 3:
        raise HTTPException(status_code=422, detail="Ask a question of at least a few letters")
    if body.meeting_id and body.folder_id:
        raise HTTPException(status_code=422, detail="Ask about one meeting or one folder, not both")
    meeting = _get_meeting_or_404(db, body.meeting_id, user, write=False) if body.meeting_id else None
    within = None
    if body.folder_id:
        try:
            within = folders.ordered(db, folders.get_owned(db, user, body.folder_id).id)
        except folders.FolderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    context, tags, searched = ask.build_context(db, user, question, meeting, within=within)
    if not context:
        return {
            "answer": "There is nothing to search yet. Upload and process a recording first, "
                      "then ask about it here.",
            "sources": [],
            "searched": searched,
        }
    prompt = qa.build_prompt(
        question,
        context,
        today=datetime.now().strftime("%A %d %B %Y"),
        user_name=user.name or user.email,
        history=ask.history_text([t.model_dump() for t in body.history]),
    )
    try:
        answer = await qa.answer(prompt)
    except QAError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    sources = ask.sources_for(answer, tags)
    saved = _save_ask_turn(db, user, meeting.id if meeting else None, question, answer, sources, searched,
                           folder_id=body.folder_id)
    return {"answer": answer, "sources": sources, "searched": searched,
            "id": saved.id, "created_at": saved.created_at.isoformat()}


ASK_HISTORY_KEEP = 100      # newest turns kept per person and scope
ASK_HISTORY_SHOWN = 50


def _ask_scope(meeting_id: Optional[str], folder_id: Optional[str]):
    """Which saved conversation: one meeting, one folder, or (neither) all meetings."""
    if meeting_id:
        return AskHistory.meeting_id == meeting_id
    if folder_id:
        return AskHistory.folder_id == folder_id
    return (AskHistory.meeting_id.is_(None)) & (AskHistory.folder_id.is_(None))


def _save_ask_turn(db: Session, user: User, meeting_id: Optional[str], question: str,
                   answer: str, sources: list, searched: int, folder_id: Optional[str] = None) -> AskHistory:
    row = AskHistory(id=str(uuid.uuid4()), user_id=user.id, meeting_id=meeting_id,
                     folder_id=None if meeting_id else folder_id, question=question,
                     answer=answer, sources=sources, searched=searched, created_at=datetime.utcnow())
    db.add(row)
    scope = _ask_scope(meeting_id, folder_id)
    old = (db.query(AskHistory.id).filter(AskHistory.user_id == user.id, scope)
           .order_by(AskHistory.created_at.desc(), AskHistory.id).offset(ASK_HISTORY_KEEP).all())
    if old:
        db.query(AskHistory).filter(AskHistory.id.in_([r[0] for r in old])).delete(synchronize_session=False)
    db.commit()
    return row


@api.get("/api/v1/ask/history")
async def ask_history(
    meeting_id: Optional[str] = Query(default=None, max_length=64),
    folder_id: Optional[str] = Query(default=None, max_length=64),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The signed-in user's saved questions and answers for one scope (a meeting, a folder, or all meetings)."""
    if meeting_id:
        _get_meeting_or_404(db, meeting_id, user, write=False)
    elif folder_id:
        try:
            folders.get_owned(db, user, folder_id)
        except folders.FolderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
    scope = _ask_scope(meeting_id, folder_id)
    rows = (db.query(AskHistory).filter(AskHistory.user_id == user.id, scope)
            .order_by(AskHistory.created_at.desc(), AskHistory.id).limit(ASK_HISTORY_SHOWN).all())
    return {"turns": [
        {"id": r.id, "question": r.question, "answer": r.answer, "sources": r.sources or [],
         "searched": r.searched, "created_at": r.created_at.isoformat()}
        for r in reversed(rows)
    ]}


@api.delete("/api/v1/ask/history", status_code=204)
async def clear_ask_history(
    meeting_id: Optional[str] = Query(default=None, max_length=64),
    folder_id: Optional[str] = Query(default=None, max_length=64),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete the signed-in user's saved conversation for one scope."""
    scope = _ask_scope(meeting_id, folder_id)
    db.query(AskHistory).filter(AskHistory.user_id == user.id, scope).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)


@api.get("/api/v1/meetings/{meeting_id}/transcript")
async def get_transcript(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the latest saved transcript for a meeting."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)

    transcript = (
        db.query(Transcript)
        .filter(Transcript.meeting_id == meeting_id)
        .order_by(Transcript.created_at.desc())
        .first()
    )

    if transcript is None:
        raise HTTPException(
            status_code=404,
            detail="No transcript found for this meeting",
        )

    return {
        "meeting_id": meeting_id,
        "transcript_id": transcript.id,
        "language": transcript.language,
        "transcript": transcript.full_text,
        "word_count": transcript.word_count,
        "speaker_count": transcript.speaker_count,
        "segments": transcript.segments,
        "created_at": (
            transcript.created_at.isoformat()
            if transcript.created_at
            else None
        ),
    }


# Summarization endpoints

def _summary_to_dict(meeting: Meeting, record: Summary) -> dict:
    return {
        "meeting_id": meeting.id,
        "summary_id": record.id,
        "meeting_title": meeting.title,
        "executive_summary": record.executive_summary,
        "key_points": record.key_points or [],
        "decisions": record.decisions or [],
        "next_steps": record.next_steps or [],
        "key_quotes": record.key_quotes or [],
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@api.post("/api/v1/meetings/{meeting_id}/summarize")
async def generate_summary(
    meeting_id: str,
    regenerate: bool = False,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """Generate a meeting summary with Ollama and save it to PostgreSQL."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    try:
        record, existed = await pipeline.summarize_step(
            db, meeting, regenerate=regenerate, summarizer=summarizer
        )
    except pipeline.PipelineError as exc:
        db.rollback()
        logger.error("Summarization failed: %s", exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        db.rollback()
        logger.exception("Summarization failed")
        raise HTTPException(
            status_code=500,
            detail="Summary generation or database save failed",
        )

    body = {**_summary_to_dict(meeting, record), "saved_to_database": True}
    if existed:
        body["already_existed"] = True
    return body


@api.get("/api/v1/meetings/{meeting_id}/summary")
async def get_summary(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve the latest saved summary from PostgreSQL."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)

    summary_record = (
        db.query(Summary)
        .filter(Summary.meeting_id == meeting_id)
        .order_by(Summary.created_at.desc())
        .first()
    )

    if summary_record is None:
        raise HTTPException(
            status_code=404,
            detail="No summary found. Generate a summary first.",
        )

    return {
        "meeting_id": meeting_id,
        "summary_id": summary_record.id,
        "meeting_title": meeting.title,
        "executive_summary": summary_record.executive_summary,
        "key_points": summary_record.key_points or [],
        "decisions": summary_record.decisions or [],
        "next_steps": summary_record.next_steps or [],
        "key_quotes": summary_record.key_quotes or [],
        "follow_up": summary_record.follow_up,
        "created_at": (
            summary_record.created_at.isoformat()
            if summary_record.created_at
            else None
        ),
    }


# Action item endpoints

def _meeting_reference_date(meeting: Meeting):
    """The actual meeting date, used to resolve relative deadlines."""
    for value in (meeting.started_at, meeting.scheduled_time, meeting.created_at):
        if value is not None:
            return value.date()
    return None


def _task_key(task: str) -> str:
    return " ".join(
        "".join(c if c.isalnum() else " " for c in (task or "").lower()).split()
    )


def _action_to_dict(item: ActionItemDB) -> dict:
    return {
        "id": item.id,
        "task": item.task,
        "assignee": item.assignee,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        # The spoken deadline that could not become a date ("soon"): the task needs a date from a person.
        "due_phrase": item.due_phrase if item.due_date is None else None,
        "needs_review": bool(item.due_phrase) and item.due_date is None and item.status != "completed",
        "priority": item.priority,
        "status": item.status,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "completed_at": (
            item.completed_at.isoformat() if item.completed_at else None
        ),
    }


@api.post("/api/v1/meetings/{meeting_id}/extract-actions")
async def extract_actions(
    meeting_id: str,
    user: User = Depends(limit_expensive),
    db: Session = Depends(get_db),
):
    """Extract action items from the latest saved transcript and persist them."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    try:
        outcome = await pipeline.extract_actions_step(
            db, meeting, extractor=action_extractor
        )
    except pipeline.PipelineError as exc:
        db.rollback()
        logger.error("Action extraction failed: %s", exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        db.rollback()
        logger.exception("Action extraction failed")
        raise HTTPException(
            status_code=500,
            detail="Action extraction or database save failed",
        )

    saved = outcome["saved"]
    return {
        "meeting_id": meeting_id,
        "action_items": [_action_to_dict(item) for item in saved],
        "count": len(saved),
        "skipped_duplicates": outcome["skipped_duplicates"],
        "unresolved_deadlines": outcome["unresolved_deadlines"],
        "saved_to_database": True,
    }


@api.get("/api/v1/meetings/{meeting_id}/actions")
async def get_actions(
    meeting_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve action items saved for a meeting."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)

    action_items = (
        db.query(ActionItemDB)
        .filter(ActionItemDB.meeting_id == meeting_id)
        .order_by(ActionItemDB.created_at.desc())
        .all()
    )

    ctx = assignments.Context(db, user)
    ctx.preload(action_items)
    return {
        "meeting_id": meeting_id,
        "action_items": [
            {**_action_to_dict(item), **ctx.view(meeting, item)} for item in action_items
        ],
        "count": len(action_items),
    }


@api.patch("/api/v1/meetings/{meeting_id}/actions/{action_id}")
async def update_action_status(
    meeting_id: str,
    action_id: str,
    status: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the status of a saved action item."""
    allowed_statuses = {"pending", "in_progress", "completed"}

    if status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail="Status must be pending, in_progress, or completed",
        )

    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)

    action = _get_action_or_404(db, meeting_id, action_id)
    _require_status_permission(db, user, meeting, action)

    if status == "completed":
        if action.status != "completed" or action.completed_at is None:
            action.completed_at = datetime.now()
    else:
        action.completed_at = None
    action.status = status

    try:
        db.commit()
        db.refresh(action)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Failed to update action item",
        )

    return {
        "meeting_id": meeting_id,
        "action_id": action.id,
        "task": action.task,
        "status": action.status,
        "updated": True,
    }


class CommentBody(BaseModel):
    body: str = Field(max_length=task_thread.MAX_COMMENT * 2)


class ExtensionBody(BaseModel):
    requested_due: datetime
    reason: Optional[str] = Field(default=None, max_length=task_thread.MAX_REASON * 2)


class DecisionBody(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=task_thread.MAX_REASON * 2)


def _thread_target(db: Session, meeting_id: str, action_id: str, user: User):
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    return meeting, _get_action_or_404(db, meeting_id, action_id)


def _thread_call(fn, db: Session, *args, **kwargs):
    try:
        return fn(db, *args, **kwargs)
    except task_thread.ThreadError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@api.get("/api/v1/meetings/{meeting_id}/actions/{action_id}/thread")
async def get_task_thread(
    meeting_id: str, action_id: str,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """The comments and deadline requests on a task, and what the signed-in user may do."""
    meeting, action = _thread_target(db, meeting_id, action_id, user)
    return task_thread.thread(db, user, meeting, action)


@api.post("/api/v1/meetings/{meeting_id}/actions/{action_id}/comments", status_code=201)
async def add_task_comment(
    meeting_id: str, action_id: str, body: CommentBody,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    meeting, action = _thread_target(db, meeting_id, action_id, user)
    c = _thread_call(task_thread.add_comment, db, user, meeting, action, body.body)
    return task_thread.view(c, {user.id: user}, user)


@api.delete("/api/v1/meetings/{meeting_id}/actions/{action_id}/comments/{comment_id}", status_code=204)
async def delete_task_comment(
    meeting_id: str, action_id: str, comment_id: str,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    meeting, action = _thread_target(db, meeting_id, action_id, user)
    row = (db.query(TaskComment).filter(TaskComment.id == comment_id, TaskComment.action_id == action.id).first()
           if _safe_id(comment_id) else None)
    if row is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    _thread_call(task_thread.delete_comment, db, user, meeting, row)
    return Response(status_code=204)


@api.post("/api/v1/meetings/{meeting_id}/actions/{action_id}/extension", status_code=201)
async def request_task_extension(
    meeting_id: str, action_id: str, body: ExtensionBody,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """The person a task is assigned to asks for a later deadline."""
    meeting, action = _thread_target(db, meeting_id, action_id, user)
    c = _thread_call(task_thread.request_extension, db, user, meeting, action, body.requested_due, body.reason)
    return task_thread.view(c, {user.id: user}, user)


@api.post("/api/v1/meetings/{meeting_id}/actions/{action_id}/extension/{request_id}/decision")
async def decide_task_extension(
    meeting_id: str, action_id: str, request_id: str, body: DecisionBody,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """The meeting owner approves (moving the deadline) or declines a request."""
    meeting, action = _thread_target(db, meeting_id, action_id, user)
    _thread_call(task_thread.decide, db, user, meeting, action, request_id, body.approve, body.note)
    db.refresh(action)
    return {**_action_to_dict(action), **assignments.Context(db, user).view(meeting, action),
            **task_thread.thread(db, user, meeting, action)}


class ActionItemCreate(BaseModel):
    task: str = Field(min_length=1, max_length=2000)
    assignee: str = "TBD"
    due_date: Optional[datetime] = None
    priority: str = Field(default="medium", pattern="^(low|medium|high)$")


class ActionItemUpdate(BaseModel):
    task: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    assignee: Optional[str] = None
    due_date: Optional[datetime] = None
    clear_due_date: bool = False
    priority: Optional[str] = Field(default=None, pattern="^(low|medium|high)$")
    status: Optional[str] = Field(
        default=None, pattern="^(pending|in_progress|completed)$"
    )


def _safe_id(value: str) -> Optional[str]:
    """IDs are UUID-like; anything else (NUL bytes, huge strings) cannot match."""
    if value and "\x00" not in value and len(value) <= 64:
        return value
    return None


def _get_meeting_or_404(
    db: Session, meeting_id: str, user: User, write: bool = False
) -> Meeting:
    """The meeting if the user may access it; 404 if not, 403 if read-only."""
    meeting = None
    if _safe_id(meeting_id):
        meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    return auth.authorize_meeting(user, meeting, write=write)


def _require_status_permission(db: Session, user: User, meeting: Meeting, action) -> None:
    """403 unless the user may tick this task (see services/assignments.py)."""
    peers = assignments.peer_first_names(db, meeting, user)
    if not assignments.can_update_status(user, meeting, action.assignee, peers):
        who = (action.assignee or "").strip()
        raise HTTPException(
            status_code=403,
            detail=(f"Only {who} can change this task" if who and not assignments.is_unclaimed(who)
                    else "Only the meeting owner can change this task"),
        )


def _get_action_or_404(db: Session, meeting_id: str, action_id: str):
    if not _safe_id(action_id):
        raise HTTPException(status_code=404, detail="Action item not found")
    action = (
        db.query(ActionItemDB)
        .filter(
            ActionItemDB.id == action_id,
            ActionItemDB.meeting_id == meeting_id,
        )
        .first()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Action item not found")
    return action


@api.post("/api/v1/meetings/{meeting_id}/actions", status_code=201)
async def create_action(
    meeting_id: str,
    body: ActionItemCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Manually add an action item to a meeting."""
    meeting = _get_meeting_or_404(db, meeting_id, user, write=True)
    scheduling.advisory_lock(db, f"actions:{meeting_id}")

    key = _task_key(body.task)
    for (existing,) in (
        db.query(ActionItemDB.task)
        .filter(ActionItemDB.meeting_id == meeting_id)
        .all()
    ):
        if _task_key(existing) == key:
            raise HTTPException(
                status_code=409,
                detail="An identical action item already exists",
            )

    record = ActionItemDB(
        id=str(uuid.uuid4()),
        meeting_id=meeting_id,
        task=body.task.strip(),
        assignee=(body.assignee or "TBD").strip() or "TBD",
        due_date=body.due_date,
        priority=body.priority,
        status="pending",
        created_at=datetime.now(),
    )
    try:
        db.add(record)
        db.commit()
        db.refresh(record)
    except Exception:
        db.rollback()
        logger.exception("Failed to create action item")
        raise HTTPException(status_code=500, detail="Failed to save action item")
    notifications.notify_assigned(db, meeting, [record], user)
    return {**_action_to_dict(record), **assignments.Context(db, user).view(meeting, record)}


@api.get("/api/v1/meetings/{meeting_id}/actions/{action_id}")
async def get_action(
    meeting_id: str,
    action_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    meeting = _get_meeting_or_404(db, meeting_id, user, write=False)
    item = _get_action_or_404(db, meeting_id, action_id)
    return {**_action_to_dict(item), **assignments.Context(db, user).view(meeting, item)}


@api.put("/api/v1/meetings/{meeting_id}/actions/{action_id}")
async def update_action(
    meeting_id: str,
    action_id: str,
    body: ActionItemUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Edit an action item (only the supplied fields change). Editing the task,
    assignee, deadline or priority needs the meeting owner; changing the
    status is up to the person the task is assigned to.
    """
    edits = (
        any(v is not None for v in (body.task, body.assignee, body.priority, body.due_date))
        or body.clear_due_date
    )
    meeting = _get_meeting_or_404(
        db, meeting_id, user, write=edits or body.status is None
    )
    action = _get_action_or_404(db, meeting_id, action_id)
    if body.status is not None:
        _require_status_permission(db, user, meeting, action)

    before_assignee = action.assignee
    if body.task is not None:
        action.task = body.task.strip()
    if body.assignee is not None:
        action.assignee = body.assignee.strip() or "TBD"
    if body.priority is not None:
        action.priority = body.priority
    if body.clear_due_date:
        action.due_date = None
        action.due_phrase = None
    elif body.due_date is not None:
        action.due_date = body.due_date
        action.due_phrase = None                 # a person has now chosen the date
    if body.status is not None:
        if body.status == "completed":
            if action.status != "completed" or action.completed_at is None:
                action.completed_at = datetime.now()
        else:
            action.completed_at = None
        action.status = body.status

    try:
        db.commit()
        db.refresh(action)
    except Exception:
        db.rollback()
        logger.exception("Failed to update action item")
        raise HTTPException(status_code=500, detail="Failed to update action item")
    if assignments.norm(action.assignee) != assignments.norm(before_assignee):
        notifications.notify_assigned(db, meeting, [action], user)
    return {**_action_to_dict(action), **assignments.Context(db, user).view(meeting, action)}


@api.delete("/api/v1/meetings/{meeting_id}/actions/{action_id}")
async def delete_action(
    meeting_id: str,
    action_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_meeting_or_404(db, meeting_id, user, write=True)
    action = _get_action_or_404(db, meeting_id, action_id)
    try:
        db.query(TaskComment).filter(TaskComment.action_id == action.id).delete(synchronize_session=False)
        db.delete(action)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to delete action item")
        raise HTTPException(status_code=500, detail="Failed to delete action item")
    return {"deleted": True, "action_id": action_id}


# Calendar endpoints

def _require_self_or_admin(user: User, user_email: str) -> None:
    if user_email.strip().lower() != user.email.lower() and not auth.is_admin(user):
        raise HTTPException(
            status_code=403, detail="You can only view your own calendar"
        )


def _accessible_meeting_ids(db: Session, user: User) -> set:
    rows = db.query(Meeting).all()
    return {m.id for m in rows if auth.can_read(user, m)}


@api.post("/api/v1/calendar/find-optimal-time")
async def find_optimal_time(
    request: FindTimeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Suggest free slots for all participants from the internal calendar."""
    try:
        slots = await scheduler.find_optimal_time(
            db,
            participants=request.participants,
            duration_minutes=request.duration_minutes,
            preferred_days=request.preferred_days,
            time_range=request.time_range,
        )
    except scheduling.SchedulingError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except NoAvailableSlotError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    best = slots[0]
    return {
        "optimal_time": best.start_time.isoformat(),
        "end_time": best.end_time.isoformat(),
        "confidence": best.confidence,
        "reasoning": best.reasoning,
        "alternatives": [
            {
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "confidence": s.confidence,
                "reasoning": s.reasoning,
            }
            for s in slots[1:]
        ],
    }


@api.get("/api/v1/calendar/{user_email}/health")
async def analyze_calendar(
    user_email: str,
    days: int = Query(default=28, ge=7, le=180),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Meeting-load metrics from the user's stored meetings."""
    _require_self_or_admin(user, user_email)
    health = await scheduler.analyze_calendar(db, user_email, days=days)

    return {
        "user_email": user_email,
        "period_days": days,
        "meeting_hours_per_week": health.meeting_hours_per_week,
        "meeting_count_per_week": health.meeting_count_per_week,
        "average_meeting_duration": health.average_meeting_duration,
        "back_to_back_meetings": health.back_to_back_meetings,
        "focus_time_blocks": health.focus_time_blocks,
        "fragmentation_score": health.fragmentation_score,
        "recommendations": [
            r.model_dump() for r in await scheduler.get_recommendations(health)
        ],
    }


@api.get("/api/v1/calendar/{user_email}/events")
async def get_calendar_events(
    user_email: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    max_results: int = Query(default=50, ge=1, le=250),
    start_date: Optional[str] = Query(
        default=None, description="YYYY-MM-DD or ISO datetime (inclusive)"
    ),
    end_date: Optional[str] = Query(
        default=None, description="YYYY-MM-DD (whole day) or ISO datetime"
    ),
    event_type: Optional[str] = Query(
        default=None, description="meeting, submission (comma-separated)"
    ),
    status: Optional[str] = Query(
        default=None, description="e.g. pending,completed,scheduled"
    ),
    meeting_id: Optional[str] = None,
):
    """
    Internal calendar: scheduled meetings and action-item deadlines stored
    by this application. No external calendar is contacted.
    """
    _require_self_or_admin(user, user_email)
    try:
        events = internal_calendar.get_events(
            db,
            start=internal_calendar.parse_bound(start_date, end=False),
            end=internal_calendar.parse_bound(end_date, end=True),
            event_types=internal_calendar.parse_csv(event_type),
            statuses=internal_calendar.parse_csv(status),
            meeting_id=meeting_id,
            meeting_ids=_accessible_meeting_ids(db, user),
        )
    except internal_calendar.CalendarFilterError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        db.rollback()
        logger.exception("Internal calendar event retrieval failed")
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve internal calendar events. Check server logs.",
        ) from exc

    page = events[:max_results]
    return {
        "user_email": user_email,
        "calendar_id": internal_calendar.CALENDAR_ID,
        "count": len(page),
        "total": len(events),
        "events": page,
    }


app.include_router(api)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)