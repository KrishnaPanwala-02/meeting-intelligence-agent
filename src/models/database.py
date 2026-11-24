
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    Boolean,
    Text,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class Meeting(Base):
    """Meeting model"""

    __tablename__ = "meetings"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    status = Column(String, default="scheduled")
    scheduled_time = Column(DateTime)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    duration_minutes = Column(Integer)
    participants = Column(JSON)
    audio_file_path = Column(String, nullable=True)
    audio_original_filename = Column(String, nullable=True)
    audio_size_bytes = Column(Integer, nullable=True)
    timezone = Column(String, nullable=True)            # zone the scheduled wall-clock time is in
    series_id = Column(String, nullable=True, index=True)   # meetings repeated from one schedule
    recurrence = Column(String, nullable=True)          # daily / weekdays / weekly / biweekly / monthly
    source = Column(String, nullable=True)              # upload | browser_mic | online_tab: how the audio was made
    consent_at = Column(DateTime, nullable=True)        # when the recorder confirmed everyone knew it was recorded
    owner_email = Column(String, nullable=True, index=True)
    # The owner's folder this meeting belongs to: later meetings in it remember the earlier ones.
    folder_id = Column(String, ForeignKey("folders.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transcripts = relationship("Transcript", back_populates="meeting")
    action_items = relationship("ActionItemDB", back_populates="meeting")
    folder = relationship("Folder", lazy="joined")


class Folder(Base):
    """A named group of the owner's meetings (a project or a recurring series) that remember each other."""

    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("owner_id", "name_key", name="uq_folder_owner_name"),)

    id = Column(String, primary_key=True)
    owner_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    name_key = Column(String, nullable=False)           # name, lower case with spaces collapsed: names are unique per owner
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class User(Base):
    """Application user (login account)."""

    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=True)  # full name; used to match speakers and tasks
    timezone = Column(String, nullable=True)            # IANA name, e.g. "Asia/Kolkata"
    email_notifications = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuthSession(Base):
    """One login session. Access tokens are valid only while it is active."""

    __tablename__ = "auth_sessions"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)


class RefreshToken(Base):
    """Single-use refresh token (only its SHA-256 hash is stored)."""

    __tablename__ = "refresh_tokens"

    id = Column(String, primary_key=True)
    session_id = Column(String, ForeignKey("auth_sessions.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class PasswordResetToken(Base):
    """Single-use password-reset token (only its SHA-256 hash is stored)."""

    __tablename__ = "password_reset_tokens"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class Transcript(Base):
    """Transcript model"""

    __tablename__ = "transcripts"

    id = Column(String, primary_key=True)
    meeting_id = Column(String, ForeignKey("meetings.id"))
    language = Column(String, default="en")
    full_text = Column(Text)
    segments = Column(JSON)
    word_count = Column(Integer)
    speaker_count = Column(Integer)
    # {speaker label: voice embedding}; lets a speaker be named once and
    # recognised in later meetings. Absent for transcripts made before voices.
    speaker_voices = Column(JSON, nullable=True)
    audio_file_path = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    meeting = relationship("Meeting", back_populates="transcripts")


class VoiceProfile(Base):
    """A named voice (embedding samples) owned by the user who enrolled it."""

    __tablename__ = "voice_profiles"

    id = Column(String, primary_key=True)
    owner_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    display_name = Column(String, nullable=False)
    embeddings = Column(JSON, nullable=False)  # list of vectors, newest last
    # Where this person signs in: meetings where their voice is recognised are shared with it.
    email = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Notification(Base):
    """Something a person should know about (a task, a shared meeting, a deadline)."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key", name="uq_notification_dedupe"),)

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String, nullable=False)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    link = Column(String, nullable=True)
    meeting_id = Column(String, nullable=True, index=True)
    dedupe_key = Column(String, nullable=True)           # stops the same reminder being sent twice
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    read_at = Column(DateTime, nullable=True)
    emailed_at = Column(DateTime, nullable=True)


class TaskComment(Base):
    """A comment on a task, or a request to move its deadline (and the answer to it)."""

    __tablename__ = "task_comments"

    id = Column(String, primary_key=True)
    action_id = Column(String, ForeignKey("action_items.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    kind = Column(String, nullable=False, default="comment")     # comment | extension_request
    body = Column(Text, nullable=True)
    requested_due = Column(DateTime, nullable=True)
    status = Column(String, nullable=True)                       # pending | approved | declined
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_note = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class AskHistory(Base):
    """One question asked on the Ask page, with its answer (private to the user who asked)."""

    __tablename__ = "ask_history"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    meeting_id = Column(String, nullable=True, index=True)   # None: asked across all meetings
    folder_id = Column(String, nullable=True, index=True)    # set: asked across one folder
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    sources = Column(JSON, nullable=True)
    searched = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class Summary(Base):
    """Meeting summary model"""

    __tablename__ = "summaries"

    id = Column(String, primary_key=True)
    meeting_id = Column(String, ForeignKey("meetings.id"))
    executive_summary = Column(Text)
    key_points = Column(JSON)
    decisions = Column(JSON)
    next_steps = Column(JSON)
    key_quotes = Column(JSON)
    follow_up = Column(Text, nullable=True)   # how this meeting continues the earlier ones in its folder
    created_at = Column(DateTime, default=datetime.utcnow)


class ActionItemDB(Base):
    """Action item model"""

    __tablename__ = "action_items"

    id = Column(String, primary_key=True)
    meeting_id = Column(String, ForeignKey("meetings.id"))
    task = Column(Text, nullable=False)
    assignee = Column(String)
    due_date = Column(DateTime)
    priority = Column(String, default="medium")
    due_phrase = Column(String, nullable=True)   # the spoken deadline that could not become a date ("soon")
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    meeting = relationship("Meeting", back_populates="action_items")


class CalendarAccount(Base):
    """Connected Google Calendar or Microsoft Outlook account."""

    __tablename__ = "calendar_accounts"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_account_id",
            name="uq_calendar_provider_account",
        ),
    )

    id = Column(String, primary_key=True)
    user_email = Column(String, nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    provider_account_id = Column(String, nullable=False)
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    scopes = Column(JSON, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
