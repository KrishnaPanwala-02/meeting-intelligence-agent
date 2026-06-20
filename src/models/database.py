"""SQLAlchemy Database Models"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class Meeting(Base):
    """Meeting model"""
    __tablename__ = "meetings"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    status = Column(String, default="scheduled")  # scheduled, in_progress, completed
    scheduled_time = Column(DateTime)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    duration_minutes = Column(Integer)
    participants = Column(JSON)  # List of participant emails
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    transcripts = relationship("Transcript", back_populates="meeting")
    action_items = relationship("ActionItemDB", back_populates="meeting")


class Transcript(Base):
    """Transcript model"""
    __tablename__ = "transcripts"

    id = Column(String, primary_key=True)
    meeting_id = Column(String, ForeignKey("meetings.id"))
    language = Column(String, default="en")
    full_text = Column(Text)
    segments = Column(JSON)  # List of transcript segments
    word_count = Column(Integer)
    speaker_count = Column(Integer)
    audio_file_path = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    meeting = relationship("Meeting", back_populates="transcripts")


class Summary(Base):
    """Meeting summary model"""
    __tablename__ = "summaries"

    id = Column(String, primary_key=True)
    meeting_id = Column(String, ForeignKey="meetings.id"))
    executive_summary = Column(Text)
    key_points = Column(JSON)
    decisions = Column(JSON)
    next_steps = Column(JSON)
    key_quotes = Column(JSON)
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
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    # Relationships
    meeting = relationship("Meeting", back_populates="action_items")
