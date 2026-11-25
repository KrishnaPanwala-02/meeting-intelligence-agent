"""
Speaker names and voice recognition.

Diarization labels speakers "Speaker 1", "Speaker 2"... within one meeting.
A voice profile stores the voice embeddings of a named person, so the same
voice is labelled with that name in later meetings. Naming a speaker also
rewrites the label everywhere it was already used (transcript, summary,
action items).

Matching is deliberately conservative: below the threshold a speaker stays
"Speaker N" rather than being given a wrong name.
"""

import math
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.database import (ActionItemDB, Meeting, Summary, Transcript,
                               User, VoiceProfile)

MAX_NAME_LENGTH = 100
MAX_SAMPLES = 5                      # embeddings kept per person
# Online meetings: a voice is only named when its best match beats the runner-up by this much.
ONLINE_MARGIN = 0.08

DEFAULT_LABEL = re.compile(r"^Speaker \d+$")


class VoiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def confirm_min() -> float:
    """Voices at least this close (but below the automatic threshold) are offered for confirmation."""
    try:
        return float(os.getenv("VOICE_CONFIRM_MIN", "0.5"))
    except ValueError:
        return 0.5


def match_scope() -> str:
    """
    "all" (default): a meeting is checked against every member's own saved voice, so nobody has to be
    listed first. "participants": only the owner's voices and those of the people listed on the meeting.
    """
    return "participants" if os.getenv("VOICE_MATCH_SCOPE", "all").strip().lower() == "participants" else "all"


def threshold() -> float:
    try:
        return float(os.getenv("VOICE_MATCH_THRESHOLD", "0.6"))
    except ValueError:
        return 0.6


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def clean_name(name: Optional[str]) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise VoiceError("Name cannot be empty", 422)
    if len(cleaned) > MAX_NAME_LENGTH:
        raise VoiceError(f"Name must be at most {MAX_NAME_LENGTH} characters", 422)
    if any(not ch.isprintable() for ch in cleaned):
        raise VoiceError("Name contains invalid characters", 422)
    return cleaned


def score(profile: VoiceProfile, embedding: List[float]) -> float:
    """Best similarity between a voice and any saved sample of the person."""
    return max((cosine(s, embedding) for s in profile.embeddings or []), default=0.0)


def match_speakers(
    profiles: List[VoiceProfile],
    speaker_voices: Dict[str, List[float]],
    min_score: Optional[float] = None,
    margin: float = 0.0,
) -> Dict[str, str]:
    """
    {speaker label: person name}. Best pairs first; each person and each
    speaker is used at most once, so two voices never get the same name.
    With a margin, a voice is only named when its best match beats the runner-up by that much: when two
    people fit about equally well it stays unnamed rather than guessing.
    """
    min_score = threshold() if min_score is None else min_score
    pairs = []
    for label, emb in (speaker_voices or {}).items():
        scored = sorted(((score(p, emb), p.display_name) for p in profiles), reverse=True)
        if margin and len(scored) > 1 and scored[0][0] - scored[1][0] < margin:
            continue
        for s, name in scored:
            if s >= min_score:
                pairs.append((s, label, name))
    result: Dict[str, str] = {}
    used_names = set()
    for _, label, name in sorted(pairs, key=lambda p: -p[0]):
        if label in result or name.lower() in used_names:
            continue
        result[label] = name
        used_names.add(name.lower())
    return result


def owner_user(db: Session, meeting: Meeting) -> Optional[User]:
    if not meeting.owner_email:
        return None
    return db.query(User).filter(User.email == meeting.owner_email).first()


def profiles_for(db: Session, owner_id: str) -> List[VoiceProfile]:
    return db.query(VoiceProfile).filter(VoiceProfile.owner_id == owner_id).all()


def find_profile(db: Session, owner_id: str, name: str) -> Optional[VoiceProfile]:
    for p in profiles_for(db, owner_id):
        if p.display_name.lower() == name.lower():
            return p
    return None


def share_with(meeting: Meeting, emails: List[Optional[str]]) -> List[str]:
    """Add people to the meeting's participants (they can then read it). Returns who was added."""
    known = {str(x).strip().lower() for x in (meeting.participants or [])}
    owner = (meeting.owner_email or "").lower()
    added: List[str] = []
    for raw in emails:
        email = (raw or "").strip().lower()
        if email and email != owner and email not in known:
            known.add(email)
            added.append(email)
    if added:
        meeting.participants = list(meeting.participants or []) + added
    return added


class _Person:
    """One person's saved voice samples, gathered from every profile that names them."""

    def __init__(self, name: str, embeddings: List[List[float]], email: Optional[str]):
        self.display_name, self.embeddings, self.email = name, embeddings, email


def directory_profiles(db: Session) -> List[VoiceProfile]:
    """The voice each active member saved of themselves (a profile in their own account, under their own e-mail)."""
    return (db.query(VoiceProfile).join(User, User.id == VoiceProfile.owner_id)
            .filter(User.is_active.is_(True), func.lower(VoiceProfile.email) == func.lower(User.email))
            .all())


def candidate_profiles(db: Session, meeting: Meeting) -> List["_Person"]:
    """
    The voices to recognise in a meeting: the owner's saved voices, plus the voice each member recorded of
    themselves. By default that is every member, so people need not be listed on the meeting first;
    VOICE_MATCH_SCOPE=participants limits it to the people listed on it.
    """
    owner = owner_user(db, meeting)
    profiles = list(profiles_for(db, owner.id)) if owner is not None else []
    if owner is not None:
        if match_scope() == "all":
            profiles += [p for p in directory_profiles(db) if p.owner_id != owner.id]
        else:
            emails = {str(p).strip().lower() for p in (meeting.participants or []) if "@" in str(p)}
            if emails:
                for u in db.query(User).filter(User.email.in_(list(emails)), User.is_active.is_(True)).all():
                    if u.id == owner.id:
                        continue
                    profiles += [p for p in profiles_for(db, u.id) if (p.email or "").lower() == u.email.lower()]
    merged: Dict[str, _Person] = {}
    for p in profiles:
        key = p.display_name.lower()
        person = merged.setdefault(key, _Person(p.display_name, [], p.email))
        person.embeddings += list(p.embeddings or [])
        person.email = person.email or p.email
    return list(merged.values())


def _key(name: Optional[str]) -> str:
    return " ".join((name or "").split()).casefold()


def people_directory(db: Session) -> List[Dict[str, str]]:
    """Members with a name: who can be picked when naming a speaker."""
    rows = (db.query(User.name, User.email).filter(User.is_active.is_(True), User.name.isnot(None))
            .order_by(User.name).all())
    return [{"name": n.strip(), "email": e} for n, e in rows if (n or "").strip()]


def email_for_name(db: Session, name: Optional[str]) -> Optional[str]:
    """The e-mail of the one member with this name (None if nobody, or more than one, has it)."""
    hits = {e for n, e in ((p["name"], p["email"]) for p in people_directory(db)) if _key(n) == _key(name)}
    return next(iter(hits)) if len(hits) == 1 else None


def best_match(people: List[Any], embedding: List[float]) -> Optional[Dict[str, Any]]:
    """The closest known person to a voice, if close enough to offer: {"name", "score", "strong"}."""
    best, best_score = None, 0.0
    for p in people:
        s = score(p, embedding)
        if s > best_score:
            best, best_score = p, s
    if best is None or best_score < confirm_min():
        return None
    return {"name": best.display_name, "score": round(best_score, 2), "strong": best_score >= threshold()}


def known_voice_samples(db: Session, meeting: Meeting) -> Dict[str, List[List[float]]]:
    """{person name: their saved voice samples} for everyone this meeting can recognise (none without an owner)."""
    if owner_user(db, meeting) is None:
        return {}
    return {p.display_name: list(p.embeddings or []) for p in candidate_profiles(db, meeting) if p.embeddings}


def apply_known_voices(db: Session, meeting: Meeting, segments: List[Dict[str, Any]],
                       speaker_voices: Optional[Dict[str, List[float]]], strict: bool = False):
    """
    Replace "Speaker N" with the names of recognised voices. Returns
    (segments, speaker_voices) with labels updated; unchanged if nothing matched.
    """
    owner = owner_user(db, meeting)
    if owner is None or not speaker_voices:
        return segments, speaker_voices
    profiles = candidate_profiles(db, meeting)
    by_name = {p.display_name.lower(): p for p in profiles}
    # Voices the transcriber already named (turn by turn) are kept; only the "Speaker N" ones are matched here.
    already = {seg.get("speaker") for seg in segments if seg.get("speaker") and not DEFAULT_LABEL.match(seg["speaker"])}
    unnamed = {k: v for k, v in speaker_voices.items() if DEFAULT_LABEL.match(k)}
    taken = {n.lower() for n in already}
    names = match_speakers([p for p in profiles if p.display_name.lower() not in taken], unnamed,
                           margin=ONLINE_MARGIN if strict else 0.0)
    if not names and not already:
        return segments, speaker_voices
    # Recognised people with a saved e-mail get access to the meeting automatically.
    meeting._newly_shared = share_with(
        meeting, [by_name[n.lower()].email for n in [*names.values(), *already] if n.lower() in by_name])
    for seg in segments:
        if seg.get("speaker") in names:
            seg["speaker"] = names[seg["speaker"]]
    return segments, {names.get(k, k): v for k, v in speaker_voices.items()}


def enroll(db: Session, owner_id: str, name: str, embedding: List[float],
           email: Optional[str] = None) -> VoiceProfile:
    """Add a voice sample to the person's profile (created on first use)."""
    existing = [p for p in profiles_for(db, owner_id)
                if p.display_name.lower() == name.lower()]
    now = datetime.utcnow()
    if existing:
        profile = existing[0]
        profile.embeddings = (list(profile.embeddings or []) + [embedding])[-MAX_SAMPLES:]
        profile.updated_at = now
        if email:
            profile.email = email
        return profile
    profile = VoiceProfile(id=str(uuid.uuid4()), owner_id=owner_id,
                           display_name=name, embeddings=[embedding], email=email or None,
                           created_at=now, updated_at=now)
    db.add(profile)
    return profile


def _replace_label(value: Any, pattern: "re.Pattern", new: str) -> Any:
    if isinstance(value, str):
        return pattern.sub(new.replace("\\", "\\\\"), value)
    if isinstance(value, list):
        return [_replace_label(v, pattern, new) for v in value]
    if isinstance(value, dict):
        return {k: _replace_label(v, pattern, new) for k, v in value.items()}
    return value


def latest_transcript(db: Session, meeting_id: str) -> Optional[Transcript]:
    return (db.query(Transcript).filter(Transcript.meeting_id == meeting_id)
            .order_by(Transcript.created_at.desc()).first())


# "My name is Megha Panwala", "I'm Rahul", "this is Anna speaking": the name is the run of
# capitalised words after the cue. Only the cue is case-insensitive.
_CUES = [
    (3, r"(?i:my name is|my name['’]s|name['’]s|call me|myself)"),
    (2, r"(?i:this is)"),
    (1, r"(?i:i am|i['’]m)"),
]
_NAME_TAIL = r"\s+(?P<name>[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,2})"
_NOT_NAMES = {
    "Fine", "Good", "Great", "Happy", "Here", "Going", "Sorry", "Glad", "Not", "Just", "Very", "Ready",
    "Sure", "Okay", "Ok", "Done", "Back", "Also", "Still", "Now", "Today", "Tomorrow", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "The", "A", "An", "So", "Well", "Yes", "No",
    "Working", "Looking", "Responsible", "Calling", "Speaking", "Planning", "Doing", "Thinking",
}


def _collapse(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _closest_known(heard: str, candidates: List[str]) -> Optional[str]:
    """A known person whose name is a near spelling of what was heard (speech-to-text errors)."""
    from difflib import SequenceMatcher
    heard_c = _collapse(heard)
    heard_first = _collapse(heard.split()[0]) if heard.split() else ""
    best, best_score = None, 0.0
    for cand in candidates:
        cand_c = _collapse(cand)
        score = SequenceMatcher(None, heard_c, cand_c).ratio()
        if len(heard.split()) == 1:                      # just a first name was said
            first = _collapse(cand.split()[0]) if cand.split() else ""
            score = max(score, SequenceMatcher(None, heard_first, first).ratio())
        if score > best_score:
            best, best_score = cand, score
    return best if best_score >= 0.8 else None


def suggest_name(texts: List[str], known_names: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    The name a speaker introduced themselves with, if any: {"name", "heard", "quote", "known"}.
    ``heard`` is the spelling in the transcript; ``name`` is the closest known person's
    spelling when there is a near match (so "Megapanvala" becomes "Megha Panwala").
    """
    best = None
    for text in texts:
        for rank, cue in _CUES:
            for m in re.finditer(cue + _NAME_TAIL, text or ""):
                words = [w.strip(".,;:!?'’-") for w in m.group("name").split()]
                while words and words[-1] in _NOT_NAMES:
                    words.pop()
                if not words or words[0] in _NOT_NAMES:
                    continue
                heard = " ".join(words)
                if best is None or rank > best[0]:
                    best = (rank, heard, text.strip())
    if best is None:
        return None
    _, heard, quote = best
    known = _closest_known(heard, [k for k in (known_names or []) if k])
    return {"name": known or heard, "heard": heard, "quote": quote[:200], "known": known is not None}


CLIPS_PER_SPEAKER = 3
CLIP_MAX_SECONDS = 12.0


def sample_clips(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    A few of a speaker's longest turns, in time order, to play so they can be recognised by ear.
    Each is {"start", "end", "text"} in seconds, and no clip is longer than CLIP_MAX_SECONDS.
    """
    usable = [p for p in parts if p["end"] > p["start"]]
    best = sorted(usable, key=lambda p: p["end"] - p["start"], reverse=True)[:CLIPS_PER_SPEAKER]
    return [{"start": round(p["start"], 2), "end": round(min(p["end"], p["start"] + CLIP_MAX_SECONDS), 2),
             "text": p["text"]}
            for p in sorted(best, key=lambda p: p["start"])]


def list_speakers(transcript: Transcript, known_names: Optional[List[str]] = None,
                  people: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = {}
    texts: Dict[str, List[str]] = {}
    parts: Dict[str, List[Dict[str, Any]]] = {}
    for seg in transcript.segments or []:
        label = seg.get("speaker")
        if not label:
            continue
        parts.setdefault(label, []).append({"start": float(seg.get("start_time", 0)),
                                            "end": float(seg.get("end_time", 0)),
                                            "text": (seg.get("text") or "").strip()})
        s = stats.setdefault(label, {"label": label, "segments": 0, "seconds": 0.0,
                                      "words": 0})
        s["segments"] += 1
        s["seconds"] += max(0.0, float(seg.get("end_time", 0)) - float(seg.get("start_time", 0)))
        s["words"] += len((seg.get("text") or "").split())
        texts.setdefault(label, []).append(seg.get("text") or "")
    voices = transcript.speaker_voices or {}
    out = []
    for label, s in stats.items():
        named = not DEFAULT_LABEL.match(label)
        out.append({**s, "seconds": round(s["seconds"], 1),
                    "named": named,
                    "has_voice": label in voices,
                    "clips": sample_clips(parts.get(label, [])),
                    # Only offered for speakers that still have a "Speaker N" label.
                    "suggestion": None if named else suggest_name(texts[label], known_names),
                    # A saved voice that sounds like this speaker: "Is this Rahul Patel?"
                    "voice_match": (best_match(people, voices[label])
                                    if (not named and people and label in voices) else None)})
    return out


def rename_speaker(db: Session, meeting: Meeting, old_label: str, new_name: str,
                   acting_user: User, remember_voice: bool = True,
                   email: Optional[str] = None, revoke_previous: bool = False) -> Dict[str, Any]:
    """
    Name a speaker: rewrite the label everywhere and remember the voice. Also used to correct a wrong
    match ("Jai Jain" -> "Darshan Patel"); revoke_previous then removes the wrongly named person's
    access to this meeting.
    """
    new_name = clean_name(new_name)
    transcript = latest_transcript(db, meeting.id)
    if transcript is None or not transcript.segments:
        raise VoiceError("This meeting has no transcript yet", 404)
    labels = {s.get("speaker") for s in transcript.segments}
    if old_label not in labels:
        raise VoiceError(f"No speaker called {old_label!r} in this meeting", 404)
    owner = owner_user(db, meeting) or acting_user
    if old_label == new_name:
        return {"renamed": False, "voice_saved": False, "label": new_name}

    previous_email = None if DEFAULT_LABEL.match(old_label) else email_for_name(db, old_label)
    voices = dict(transcript.speaker_voices or {})
    voice_saved = False
    if remember_voice and old_label in voices:
        enroll(db, owner.id, new_name, voices[old_label], email=email)
        voice_saved = True
    profile = find_profile(db, owner.id, new_name)
    if profile is not None and email:
        profile.email = email                     # also when only the e-mail is new
    # The e-mail given now, else the one saved with this person's voice, else the member with that name.
    invite = email or (profile.email if profile is not None else None) or email_for_name(db, new_name)
    newly_shared = share_with(meeting, [invite])
    revoked = None
    if revoke_previous and previous_email and previous_email.lower() not in {(invite or "").lower(), (meeting.owner_email or "").lower()}:
        before = list(meeting.participants or [])
        after = [x for x in before if str(x).strip().lower() != previous_email.lower()]
        if len(after) != len(before):
            meeting.participants = after
            revoked = previous_email

    transcript.segments = [
        {**s, "speaker": new_name} if s.get("speaker") == old_label else s
        for s in transcript.segments
    ]
    if old_label in voices:
        vec = voices.pop(old_label)
        voices.setdefault(new_name, vec)         # merging keeps the existing voice
        transcript.speaker_voices = voices
    transcript.speaker_count = len({s.get("speaker") for s in transcript.segments
                                    if s.get("speaker")})

    pattern = re.compile(r"(?<![\w])" + re.escape(old_label) + r"(?!\w)", re.IGNORECASE)
    for summary in db.query(Summary).filter(Summary.meeting_id == meeting.id).all():
        summary.executive_summary = _replace_label(summary.executive_summary, pattern, new_name)
        summary.key_points = _replace_label(summary.key_points, pattern, new_name)
        summary.decisions = _replace_label(summary.decisions, pattern, new_name)
        summary.next_steps = _replace_label(summary.next_steps, pattern, new_name)
        summary.key_quotes = _replace_label(summary.key_quotes, pattern, new_name)
    updated = 0
    reassigned = []
    for item in db.query(ActionItemDB).filter(ActionItemDB.meeting_id == meeting.id).all():
        new_assignee = _replace_label(item.assignee, pattern, new_name)
        new_task = _replace_label(item.task, pattern, new_name)
        if new_assignee != item.assignee or new_task != item.task:
            if new_assignee != item.assignee:
                reassigned.append(item)
            item.assignee, item.task = new_assignee, new_task
            updated += 1
    db.commit()
    from . import notifications   # not at the top: it would import auth, which imports the workers, which import the pipeline
    notifications.notify_shared(db, meeting, newly_shared, acting_user)
    notifications.notify_assigned(db, meeting, reassigned, acting_user)
    return {"renamed": True, "voice_saved": voice_saved, "label": new_name,
            "action_items_updated": updated, "invited": invite, "revoked": revoked}
