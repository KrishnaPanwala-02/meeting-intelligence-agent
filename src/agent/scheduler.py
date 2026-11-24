"""
Calendar Optimization Agent
Scheduling intelligence computed from the internal meeting database.
"""

from datetime import datetime
from typing import List, Optional, Dict

from loguru import logger
from sqlalchemy.orm import Session

from ..models.schemas import CalendarHealth, TimeSlot, SchedulingRecommendation
from ..services import scheduling


class NoAvailableSlotError(RuntimeError):
    """No free slot satisfies the request."""


class CalendarOptimizer:
    """
    Calendar Optimization Agent

    All results are derived from meetings stored in PostgreSQL; there is no
    external calendar and no simulated data.

    - find_optimal_time: best free slot for all participants
    - analyze_calendar: real meeting-load metrics for a user
    - get_recommendations: advice derived from those metrics
    """

    def __init__(self):
        self.working_hours_start = 9
        self.working_hours_end = 17
        logger.info("Calendar Optimizer initialized")

    async def find_optimal_time(
        self,
        db: Session,
        participants: List[str],
        duration_minutes: int,
        preferred_days: Optional[List[str]] = None,
        time_range: Optional[Dict[str, str]] = None,
        limit: int = 5,
    ) -> List[TimeSlot]:
        """Return ranked free slots (best first); raises if none exist."""
        day_start, day_end, from_date, to_date = scheduling.parse_search_window(
            time_range, self.working_hours_start, self.working_hours_end
        )
        slots = scheduling.suggest_slots(
            db,
            participants=participants,
            duration_minutes=duration_minutes,
            preferred_days=preferred_days,
            day_start=day_start,
            day_end=day_end,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        if not slots:
            raise NoAvailableSlotError(
                "No free slot found for all participants in the search window"
            )

        best_score = max(s.score for s in slots)
        return [
            TimeSlot(
                start_time=s.start,
                end_time=s.end,
                participants=participants,
                # Share of the maximum achievable score for a slot.
                confidence=round(max(0.0, min(1.0, s.score / 115.0)), 2),
                reasoning="; ".join(s.reasons),
            )
            for s in slots
        ]

    async def analyze_calendar(
        self, db: Session, user_email: str, days: int = 28
    ) -> CalendarHealth:
        """Meeting-load metrics from the user's stored meetings."""
        m = scheduling.calendar_metrics(
            db, user_email, days=days,
            day_start=datetime.min.time().replace(hour=self.working_hours_start),
            day_end=datetime.min.time().replace(hour=self.working_hours_end),
        )
        return CalendarHealth(
            user_email=user_email,
            meeting_hours_per_week=m["meeting_hours_per_week"],
            focus_time_blocks=m["focus_time_blocks"],
            fragmentation_score=m["fragmentation_score"],
            back_to_back_meetings=m["back_to_back_meetings"],
            average_meeting_duration=m["average_meeting_duration"],
            meeting_count_per_week=m["meeting_count_per_week"],
            analyzed_at=datetime.now(),
        )

    async def get_recommendations(
        self, health: CalendarHealth
    ) -> List[SchedulingRecommendation]:
        """Advice derived from real per-week metrics."""
        recommendations = []

        if health.back_to_back_meetings > 3:
            recommendations.append(SchedulingRecommendation(
                suggestion="Add buffer time between meetings",
                reasoning=(
                    f"{health.back_to_back_meetings} back-to-back meetings "
                    "per week leave no time to recover or prepare"
                ),
                estimated_time_saved=round(health.back_to_back_meetings * 0.25, 1),
                priority="high",
            ))

        if health.focus_time_blocks < 3:
            recommendations.append(SchedulingRecommendation(
                suggestion="Block focus time in your calendar",
                reasoning=(
                    f"Only {health.focus_time_blocks} uninterrupted 2-hour "
                    "blocks per week for deep work"
                ),
                estimated_time_saved=round((3 - health.focus_time_blocks) * 2.0, 1),
                priority="high",
            ))

        if health.meeting_hours_per_week > 20:
            recommendations.append(SchedulingRecommendation(
                suggestion="Reduce meeting attendance",
                reasoning=(
                    f"{health.meeting_hours_per_week} meeting hours per week "
                    "exceeds the recommended 20"
                ),
                estimated_time_saved=round(health.meeting_hours_per_week - 20, 1),
                priority="medium",
            ))

        return recommendations
