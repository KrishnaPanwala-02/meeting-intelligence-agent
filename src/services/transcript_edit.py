"""
Correcting a transcript by hand: fix one segment, or replace a mis-heard word
everywhere (transcript, summary, action items) in one go.

Speech recognition mishears names ("Megapanvala" for "Megha Panwala"); people
fix those mistakes here instead of re-recording. Text is edited in place, and
every changed segment is marked ``edited`` so the page can show it.
"""

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, Summary
from .voices import VoiceError, latest_transcript

MAX_SEGMENT_CHARS = 5000
MAX_FIND_CHARS = 200


def _rebuild(transcript) -> None:
    """Keep the joined text and word count in step with the segments."""
    transcript.full_text = " ".join(
        (s.get("text") or "").strip() for s in transcript.segments or []
    ).strip()
    transcript.word_count = sum(len((s.get("text") or "").split()) for s in transcript.segments or [])


def _require_transcript(db: Session, meeting: Meeting):
    transcript = latest_transcript(db, meeting.id)
    if transcript is None or not transcript.segments:
        raise VoiceError("This meeting has no transcript yet", 404)
    return transcript


def edit_segment(db: Session, meeting: Meeting, segment_id: int, text: str) -> Dict[str, Any]:
    """Replace the words of one transcript segment."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise VoiceError("The text cannot be empty", 422)
    if len(cleaned) > MAX_SEGMENT_CHARS:
        raise VoiceError(f"The text must be at most {MAX_SEGMENT_CHARS} characters", 422)
    if any(not ch.isprintable() for ch in cleaned):
        raise VoiceError("The text contains invalid characters", 422)

    transcript = _require_transcript(db, meeting)
    segments = [dict(s) for s in transcript.segments]
    target = next((s for s in segments if s.get("id") == segment_id), None)
    if target is None:
        raise VoiceError("Segment not found", 404)
    changed = (target.get("text") or "").strip() != cleaned
    if changed:
        target["text"] = cleaned
        target["edited"] = True
        transcript.segments = segments
        _rebuild(transcript)
        db.commit()
    return {**target, "changed": changed, "word_count": transcript.word_count}


def _sub_json(value: Any, pattern: "re.Pattern", repl: str, counter: List[int]) -> Any:
    if isinstance(value, str):
        out, n = pattern.subn(lambda _m: repl, value)
        counter[0] += n
        return out
    if isinstance(value, list):
        return [_sub_json(v, pattern, repl, counter) for v in value]
    if isinstance(value, dict):
        return {k: _sub_json(v, pattern, repl, counter) for k, v in value.items()}
    return value


def replace_everywhere(
    db: Session,
    meeting: Meeting,
    find: str,
    replace: str,
    match_case: bool = False,
    whole_word: bool = True,
    include_summary: bool = True,
) -> Dict[str, int]:
    """Replace ``find`` by ``replace`` in the transcript and, optionally, the summary and tasks."""
    find = " ".join((find or "").split())
    replace = " ".join((replace or "").split())
    if not find:
        raise VoiceError("Enter the text to find", 422)
    if len(find) > MAX_FIND_CHARS or len(replace) > MAX_FIND_CHARS:
        raise VoiceError(f"Text must be at most {MAX_FIND_CHARS} characters", 422)
    if any(not ch.isprintable() for ch in find + replace):
        raise VoiceError("The text contains invalid characters", 422)
    if find == replace:
        raise VoiceError("The replacement is the same as the text to find", 422)

    transcript = _require_transcript(db, meeting)
    body = re.escape(find)
    if whole_word:
        body = rf"(?<!\w){body}(?!\w)"
    pattern = re.compile(body, 0 if match_case else re.IGNORECASE)

    counts = {"transcript": 0, "summary": 0, "action_items": 0}

    n = [0]
    segments = []
    for seg in transcript.segments:
        seg = dict(seg)
        before = n[0]
        seg["text"] = _sub_json(seg.get("text") or "", pattern, replace, n)
        if seg.get("speaker"):
            seg["speaker"] = _sub_json(seg["speaker"], pattern, replace, n)
        if n[0] != before:
            seg["edited"] = True
        segments.append(seg)
    counts["transcript"] = n[0]
    if n[0]:
        transcript.segments = segments
        voices = transcript.speaker_voices
        if voices:   # a speaker label that was fixed keeps its voice
            renamed = {_sub_json(k, pattern, replace, [0]): v for k, v in voices.items()}
            transcript.speaker_voices = renamed
        transcript.speaker_count = len({s.get("speaker") for s in segments if s.get("speaker")})
        _rebuild(transcript)

    if include_summary:
        m = [0]
        for summary in db.query(Summary).filter(Summary.meeting_id == meeting.id).all():
            summary.executive_summary = _sub_json(summary.executive_summary, pattern, replace, m)
            summary.key_points = _sub_json(summary.key_points, pattern, replace, m)
            summary.decisions = _sub_json(summary.decisions, pattern, replace, m)
            summary.next_steps = _sub_json(summary.next_steps, pattern, replace, m)
            summary.key_quotes = _sub_json(summary.key_quotes, pattern, replace, m)
        counts["summary"] = m[0]
        a = [0]
        for item in db.query(ActionItemDB).filter(ActionItemDB.meeting_id == meeting.id).all():
            item.task = _sub_json(item.task, pattern, replace, a)
            item.assignee = _sub_json(item.assignee, pattern, replace, a)
        counts["action_items"] = a[0]

    db.commit()
    counts["total"] = sum(counts.values())
    return counts
