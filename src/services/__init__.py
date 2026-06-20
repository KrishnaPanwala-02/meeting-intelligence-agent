"""Service layer for external integrations"""

from .calendar_service import CalendarService
from .notification import NotificationService
from .audio_processor import AudioProcessor

__all__ = ["CalendarService", "NotificationService", "AudioProcessor"]
