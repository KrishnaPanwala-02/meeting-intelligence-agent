"""
Meeting-processing steps shared by the API and the Celery workers.

Each step reads and writes PostgreSQL through the session it is given and
raises PipelineError subclasses, which carry an HTTP-style status code and
say whether the failure is worth retrying.
"""

import os
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from ..agent.action_extractor import ActionExtractionError, ActionItemExtractor
from ..agent.summarizer import MeetingSummarizer, SummarizationError
from ..agent.transcriber import (
    DiarizationUnavailableError,
    MeetingTranscriber,
    TranscriptionUnavailableError,
)
from ..models.database import ActionItemDB, Meeting, Summary, Transcript
from ..models.schemas import TranscriptionResult
from . import audio_storage, folders, voices

STEPS = ("transcribe", "summarize", "extract_actions")


class PipelineError(Exception):
    """A processing step failed permanently (retrying will not help)."""

    retryable = False

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TransientPipelineError(PipelineError):
    """A dependency (e.g. Ollama) was unavailable; retrying may succeed."""

    retryable = True

    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message, status_code)


# ---------------------------------------------------------------------------
# Lazily created agents (one set per process; Whisper loads on first use)
# ---------------------------------------------------------------------------

_transcriber: Optional[MeetingTranscriber] = None


def get_transcriber() -> MeetingTranscriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = MeetingTranscriber(
            model=os.getenv("WHISPER_MODEL", "base"),
            device=os.getenv("WHISPER_DEVICE", "cpu"),
            compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        )
    return _transcriber


def ollama_settings() -> Tuple[str, str]:
    return (
        os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434"),
        os.getenv("OLLAMA_MODEL", "llama3.2"),
    )


def make_summarizer() -> MeetingSummarizer:
    host, model = ollama_settings()
    return MeetingSummarizer(ollama_host=host, model=model)


def make_extractor() -> ActionItemExtractor:
    host, model = ollama_settings()
    return ActionItemExtractor(ollama_host=host, model=model)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_meeting(db: Session, meeting_id: str) -> Meeting:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise PipelineError("Meeting not found", 404)
    return meeting


def latest_transcript(db: Session, meeting_id: str) -> Optional[Transcript]:
    return (
        db.query(Transcript)
        .filter(Transcript.meeting_id == meeting_id)
        .order_by(Transcript.created_at.desc())
        .first()
    )


def _require_transcript(db: Session, meeting_id: str) -> Transcript:
    record = latest_transcript(db, meeting_id)
    if record is None:
        raise PipelineError(
            "No transcript found. Transcribe the meeting first.", 404
        )
    if not record.segments:
        raise PipelineError(
            "The saved transcript contains no segments.", 400
        )
    return record


def transcript_to_result(record: Transcript) -> TranscriptionResult:
    return TranscriptionResult(
        audio_file=record.audio_file_path or "",
        language=record.language,
        segments=record.segments,
        duration_seconds=max(
            (float(s.get("end_time", 0)) for s in record.segments),
            default=0.0,
        ),
        word_count=record.word_count or 0,
        speaker_count=record.speaker_count or 0,
        created_at=record.created_at or datetime.now(),
    )


def meeting_reference_date(meeting: Meeting):
    """The actual meeting date, used to resolve relative deadlines."""
    for value in (meeting.started_at, meeting.scheduled_time, meeting.created_at):
        if value is not None:
            return value.date()
    return None


def task_key(task: str) -> str:
    return " ".join(
        "".join(c if c.isalnum() else " " for c in (task or "").lower()).split()
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def transcribe_step(
    db: Session,
    meeting: Meeting,
    audio_path: str,
    language: str = "en",
    identify_speakers: bool = False,
    store_path: bool = True,
    transcriber: Optional[MeetingTranscriber] = None,
):
    """Transcribe an audio file and save the transcript. Returns (row, result)."""
    transcriber = transcriber or get_transcriber()
    known = voices.known_voice_samples(db, meeting) if identify_speakers else None
    # An online meeting recorded from a browser tab keeps your microphone and the other people apart.
    online = identify_speakers and meeting.source == "online_tab"
    extra = {"known_voices": known} if known else {}
    if online:
        owner = voices.owner_user(db, meeting)
        extra.update(online=True, local_name=(owner.name or "").strip() or None if owner else None)
    try:
        result = transcriber.transcribe(
            audio_path=audio_path,
            language=language,
            identify_speakers=identify_speakers,
            **extra,
        )
    except (TranscriptionUnavailableError, DiarizationUnavailableError) as exc:
        raise PipelineError(str(exc), 503)
    except FileNotFoundError:
        raise PipelineError("The stored audio file is missing", 400)

    segments = [segment.dict() for segment in result.segments]
    # Voices already named in earlier meetings keep their names.
    segments, speaker_voices = voices.apply_known_voices(
        db, meeting, segments, result.speaker_voices, strict=online
    )
    transcript = Transcript(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        language=language,
        full_text=" ".join(s.get("text", "").strip() for s in segments).strip(),
        segments=segments,
        word_count=result.word_count,
        speaker_count=result.speaker_count,
        speaker_voices=speaker_voices,
        audio_file_path=audio_storage.canonical_path(audio_path) if store_path else None,
    )
    try:
        db.add(transcript)
        db.commit()
        db.refresh(transcript)
    except Exception:
        db.rollback()
        raise
    from . import notifications   # not at the top: notifications -> auth -> workers -> pipeline would be a loop
    notifications.notify_shared(db, meeting, getattr(meeting, "_newly_shared", []) or [], None)
    return transcript, result


def voices_step(
    db: Session,
    meeting: Meeting,
    transcriber: Optional[MeetingTranscriber] = None,
) -> dict:
    """
    Add voice embeddings to an already-transcribed meeting (made before voices
    were recorded) without touching its text, summary or tasks.
    """
    record = _require_transcript(db, meeting.id)
    audio = audio_storage.local_path(meeting.audio_file_path or record.audio_file_path)
    if not audio or not os.path.exists(audio):
        raise PipelineError("No stored audio for this meeting.", 400)
    transcriber = transcriber or get_transcriber()
    try:
        found = transcriber.extract_voices(audio, record.segments)
    except (TranscriptionUnavailableError, DiarizationUnavailableError) as exc:
        raise PipelineError(str(exc), 503)
    except FileNotFoundError:
        raise PipelineError("The stored audio file is missing", 400)
    if not found:
        raise PipelineError("Could not extract voices from this recording.", 422)
    record.speaker_voices = {**(record.speaker_voices or {}), **found}
    # Recognise people whose voices are already saved.
    names = {}
    owner = voices.owner_user(db, meeting)
    if owner is not None:
        names = voices.match_speakers(voices.candidate_profiles(db, meeting), found)
    db.commit()
    for label, name in names.items():
        if voices.DEFAULT_LABEL.match(label):
            voices.rename_speaker(db, meeting, label, name, owner, remember_voice=False)
    return {"voices": len(found), "recognised": len(names)}


async def _follow_up(db: Session, meeting: Meeting, summarizer, generated) -> Optional[str]:
    """
    For a meeting in a folder: how it continues the earlier meetings there. Best effort: if the model is
    slow or fails, the summary is still saved without it.
    """
    try:
        memory = folders.memory_text(db, meeting)
        if not memory:
            return None
        return await summarizer.follow_up(
            memory, meeting.title, folders.number_of(db, meeting), generated.executive_summary,
            [d.decision for d in generated.decisions], list(generated.next_steps or []),
        )
    except Exception as exc:
        logger.warning(f"Meeting {meeting.id}: could not relate it to the earlier meetings: {exc}")
        return None


async def summarize_step(
    db: Session,
    meeting: Meeting,
    regenerate: bool = False,
    summarizer: Optional[MeetingSummarizer] = None,
) -> Tuple[Summary, bool]:
    """Summarize the latest transcript. Returns (row, already_existed)."""
    record = _require_transcript(db, meeting.id)

    if not regenerate:
        existing = (
            db.query(Summary)
            .filter(
                Summary.meeting_id == meeting.id,
                Summary.created_at >= record.created_at,
            )
            .order_by(Summary.created_at.desc())
            .first()
        )
        if existing is not None:
            return existing, True

    summarizer = summarizer or make_summarizer()
    try:
        generated = await summarizer.summarize(
            transcript=transcript_to_result(record),
            meeting_title=meeting.title,
            participants=meeting.participants or [],
        )
    except SummarizationError as exc:
        raise TransientPipelineError(str(exc))

    summary = Summary(
        id=str(uuid.uuid4()),
        meeting_id=meeting.id,
        executive_summary=generated.executive_summary,
        key_points=[p.dict() for p in generated.key_points],
        decisions=[d.dict() for d in generated.decisions],
        next_steps=generated.next_steps,
        key_quotes=generated.key_quotes,
        follow_up=await _follow_up(db, meeting, summarizer, generated),
    )
    try:
        db.add(summary)
        db.commit()
        db.refresh(summary)
    except Exception:
        db.rollback()
        raise
    return summary, False


async def extract_actions_step(
    db: Session,
    meeting: Meeting,
    extractor: Optional[ActionItemExtractor] = None,
) -> dict:
    """Extract action items and save the new ones (duplicates are skipped)."""
    record = _require_transcript(db, meeting.id)
    extractor = extractor or make_extractor()

    try:
        extracted = await extractor.extract(
            transcript=transcript_to_result(record),
            use_ai=True,
            reference_date=meeting_reference_date(meeting),
        )
    except ActionExtractionError as exc:
        raise TransientPipelineError(str(exc))

    # Serialize concurrent extractions of the same meeting (taken after the
    # slow LLM call, so it is held only for the quick compare-and-insert).
    from . import scheduling
    scheduling.advisory_lock(db, f"actions:{meeting.id}")

    existing = {
        task_key(row.task)
        for row in db.query(ActionItemDB.task)
        .filter(ActionItemDB.meeting_id == meeting.id)
        .all()
    }

    saved: List[ActionItemDB] = []
    skipped = 0
    needs_review = []

    try:
        for item in extracted:
            task = (item.task or "").strip()
            if not task:
                continue
            key = task_key(task)
            if key in existing:
                skipped += 1
                continue
            existing.add(key)

            unresolved = bool(item.due_date_phrase and item.due_date is None)
            if unresolved:
                needs_review.append(
                    {"task": task, "due_date_phrase": item.due_date_phrase}
                )

            record_row = ActionItemDB(
                id=str(uuid.uuid4()),
                meeting_id=meeting.id,
                task=task,
                assignee=item.assignee or "TBD",
                due_date=item.due_date,
                priority=(
                    item.priority
                    if item.priority in {"low", "medium", "high"}
                    else "medium"
                ),
                status=(
                    item.status
                    if item.status in {"pending", "in_progress", "completed"}
                    else "pending"
                ),
                due_phrase=(item.due_date_phrase or "").strip()[:120] or None if unresolved else None,
                created_at=item.created_at or datetime.now(),
            )
            db.add(record_row)
            saved.append(record_row)
        db.commit()
        for row in saved:
            db.refresh(row)
    except Exception:
        db.rollback()
        raise

    from . import notifications
    notifications.notify_assigned(db, meeting, saved, None)
    if needs_review:
        owner = voices.owner_user(db, meeting)
        if owner is not None:
            n = len(needs_review)
            notifications.notify_people(
                db, meeting, [owner], "deadline_review",
                f"{n} deadline{'s need' if n != 1 else ' needs'} a date",
                f"“{meeting.title}” has task{'s' if n != 1 else ''} whose deadline could not be turned into a date.",
                "/tasks?review=1", dedupe=f"review:{meeting.id}:{len(saved)}")
    logger.info(
        "Meeting {}: saved {} action items, skipped {} duplicates",
        meeting.id, len(saved), skipped,
    )
    return {"saved": saved, "skipped_duplicates": skipped,
            "unresolved_deadlines": needs_review}
