"""
Pydantic Schemas for Request/Response validation
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


# Transcription Schemas

class TranscriptSegment(BaseModel):
    """Single transcript segment"""
    id: int
    start_time: float
    end_time: float
    text: str
    speaker: Optional[str] = None
    confidence: float = 0.0
    # Word timings are used internally to split segments at speaker changes
    # and are never serialized or stored.
    words: Optional[List[Dict[str, Any]]] = Field(default=None, exclude=True)

    class Config:
        from_attributes = True


class TranscriptionResult(BaseModel):
    """Complete transcription result"""
    audio_file: str
    language: str
    segments: List[TranscriptSegment]
    duration_seconds: float
    word_count: int
    speaker_count: int = 0
    created_at: datetime
    processing_seconds: Optional[float] = None
    # {speaker label: voice embedding}; internal, never serialized.
    speaker_voices: Optional[Dict[str, List[float]]] = Field(default=None, exclude=True)

    def export_txt(self, path: str):
        """Export to text file"""
        with open(path, 'w', encoding='utf-8') as f:
            for seg in self.segments:
                prefix = f"[{seg.speaker}] " if seg.speaker else ""
                f.write(f"{prefix}{seg.text}\n")


# Summary Schemas

class KeyPoint(BaseModel):
    """Key discussion point"""
    topic: str
    description: str


class Decision(BaseModel):
    """Decision made in meeting"""
    decision: str
    owner: str = "TBD"
    context: str = ""


class MeetingSummary(BaseModel):
    """Complete meeting summary"""
    meeting_title: str
    executive_summary: str
    key_points: List[KeyPoint]
    decisions: List[Decision]
    next_steps: List[str]
    key_quotes: List[str]
    participants: List[str]
    duration_minutes: int
    word_count: int
    created_at: datetime


# Action Item Schemas

class ActionItem(BaseModel):
    """Action item from meeting"""
    task: str
    assignee: str = "TBD"
    due_date: Optional[datetime] = None
    # Original spoken deadline wording (not stored in the database).
    due_date_phrase: Optional[str] = None
    priority: str = "medium"  # low, medium, high
    status: str = "pending"  # pending, in_progress, completed
    dependencies: List[str] = []
    created_at: datetime = Field(default_factory=datetime.now)


# Calendar Schemas

class TimeSlot(BaseModel):
    """Available time slot"""
    start_time: datetime
    end_time: datetime
    participants: List[str]
    confidence: float = 0.0
    reasoning: str = ""


class CalendarHealth(BaseModel):
    """Calendar health metrics"""
    user_email: str
    meeting_hours_per_week: float
    focus_time_blocks: int
    fragmentation_score: float  # 0-10, higher is more fragmented
    back_to_back_meetings: int
    average_meeting_duration: int
    meeting_count_per_week: int
    analyzed_at: datetime


class SchedulingRecommendation(BaseModel):
    """Calendar optimization recommendation"""
    suggestion: str
    reasoning: str
    estimated_time_saved: float  # hours per week
    priority: str = "medium"


# API Request/Response Schemas

class MeetingCreate(BaseModel):
    """Create meeting request"""
    title: str = Field(min_length=1, max_length=200)
    participants: List[str] = Field(default_factory=list, max_length=100)
    scheduled_time: Optional[datetime] = None
    duration_minutes: int = Field(default=60, ge=1, le=1440)


class MeetingResponse(BaseModel):
    """Meeting response"""
    id: str
    title: str
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class TranscribeRequest(BaseModel):
    """Transcription request"""
    language: str = "en"
    identify_speakers: bool = False


class FindTimeRequest(BaseModel):
    """Find optimal time request"""
    participants: List[str]
    duration_minutes: int
    preferred_days: Optional[List[str]] = None
    time_range: Optional[Dict[str, str]] = None
