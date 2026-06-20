"""Meeting Intelligence Agent - AI-powered meeting management and transcription"""

__version__ = "1.0.0"

from .agent import (
    MeetingTranscriber,
    MeetingSummarizer,
    ActionItemExtractor,
    CalendarOptimizer
)

__all__ = [
    "MeetingTranscriber",
    "MeetingSummarizer",
    "ActionItemExtractor",
    "CalendarOptimizer",
]
