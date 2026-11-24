"""
Prepares what the model is shown when someone asks a question about their meetings.

Only meetings the user may read are ever searched. The relevant ones (by the words
of the question, then by recency) are summarised into a compact context made of
their summary, tasks and the transcript lines that matter, each tagged [M1], [M2]...
so answers can cite them. The context is capped so it fits the model's window.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, Summary, Transcript, User
from . import assignments, auth

MAX_CONTEXT_CHARS = 9000
MAX_MEETINGS = 5
MIN_MEETINGS = 3
SEGMENTS_PER_MEETING = 8
SEGMENT_CHARS = 300
WHOLE_TRANSCRIPT_CHARS = 3500
MAX_OPEN_TASKS = 15

_STOP = set("""
a an the and or but if then than so of to in on at by for with from about into over under after before
is are was were be been being am do does did done have has had having will would can could should may might
what whats who whom whose which when where why how that this these those there here it its it's i me my mine
we us our you your he she they them his her their not no yes any all some more most last next
tell show give list find get please meeting meetings said say says discuss discussed talk talked
""".split())


def terms(question: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", (question or "").lower())
    seen, out = set(), []
    for w in words:
        if len(w) >= 3 and w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def clean(text: Any, limit: int) -> str:
    """Untrusted text made safe for the prompt: no control characters, no look-alike citation tags."""
    s = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in str(text or ""))
    s = " ".join(s.split()).replace("[", "(").replace("]", ")")
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _count(text: str, words: List[str]) -> int:
    low = (text or "").lower()
    return sum(min(low.count(w), 5) for w in words)


def meeting_when(m: Meeting) -> Optional[datetime]:
    return m.scheduled_time or m.started_at or m.created_at


def _clock(seconds: Any) -> str:
    s = max(0, int(float(seconds or 0)))
    return f"{s // 60}:{s % 60:02d}"


class _Tags:
    """[M1], [M2]... handed out the first time a meeting is mentioned."""

    def __init__(self):
        self.by_meeting: Dict[str, str] = {}
        self.meetings: Dict[str, Meeting] = {}

    def tag(self, m: Meeting) -> str:
        if m.id not in self.by_meeting:
            self.by_meeting[m.id] = f"M{len(self.by_meeting) + 1}"
            self.meetings[m.id] = m
        return self.by_meeting[m.id]


def _summary_text(s: Optional[Summary]) -> str:
    if s is None:
        return ""
    bits = [clean(s.executive_summary, 700)] if s.executive_summary else []
    for d in (s.decisions or [])[:6]:
        if isinstance(d, dict):
            bits.append(f"Decision: {clean(d.get('decision'), 200)} (owner: {clean(d.get('owner'), 60)})")
    for step in (s.next_steps or [])[:6]:
        bits.append(f"Next step: {clean(step, 200)}")
    return "\n".join(bits)


def _task_line(a: ActionItemDB, mine: bool = False) -> str:
    due = a.due_date.strftime("%Y-%m-%d") if a.due_date else "no deadline"
    flag = " (the user's task)" if mine else ""
    return f"- {clean(a.task, 200)} — assigned to {clean(a.assignee or 'TBD', 60)}; due {due}; {a.status}{flag}"


def _pick_segments(segments: List[Dict[str, Any]], words: List[str], n: int) -> List[Dict[str, Any]]:
    texts = [(s, _count(f"{s.get('speaker') or ''} {s.get('text') or ''}", words)) for s in segments]
    ranked = sorted((t for t in texts if t[1] > 0), key=lambda t: -t[1])[:n]
    if not ranked:                       # nothing matched: the opening lines give some orientation
        return segments[: min(n, 3)]
    return sorted((s for s, _ in ranked), key=lambda s: float(s.get("start_time") or 0))


def _segment_lines(segments: List[Dict[str, Any]]) -> List[str]:
    return [
        f"  [{_clock(s.get('start_time'))}] {clean(s.get('speaker') or 'Speaker', 60)}: {clean(s.get('text'), SEGMENT_CHARS)}"
        for s in segments
    ]


def build_context(
    db: Session,
    user: User,
    question: str,
    meeting: Optional[Meeting] = None,
    max_chars: int = MAX_CONTEXT_CHARS,
    within: Optional[List[Meeting]] = None,
) -> Tuple[str, "_Tags", int]:
    """Returns (context text, tags used, number of meetings searched). `within` limits the search to those meetings."""
    if meeting is not None:
        searched = [meeting]
    elif within is not None:
        searched = [m for m in within if auth.can_read(user, m)]
    else:
        searched = [m for m in db.query(Meeting).all() if auth.can_read(user, m)]
    if not searched:
        return "", _Tags(), 0

    ids = [m.id for m in searched]
    transcripts: Dict[str, Transcript] = {}
    for t in db.query(Transcript).filter(Transcript.meeting_id.in_(ids)).order_by(Transcript.created_at):
        transcripts[t.meeting_id] = t                       # latest wins
    summaries: Dict[str, Summary] = {}
    for s in db.query(Summary).filter(Summary.meeting_id.in_(ids)).order_by(Summary.created_at):
        summaries[s.meeting_id] = s
    actions: Dict[str, List[ActionItemDB]] = {}
    for a in db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(ids)):
        actions.setdefault(a.meeting_id, []).append(a)

    words = terms(question)

    def score(m: Meeting) -> int:
        t = transcripts.get(m.id)
        seg_text = " ".join(f"{s.get('speaker') or ''} {s.get('text') or ''}" for s in (t.segments if t else []) or [])
        act_text = " ".join(f"{a.task} {a.assignee}" for a in actions.get(m.id, []))
        return (3 * _count(m.title, words) + 2 * _count(_summary_text(summaries.get(m.id)), words)
                + 3 * _count(act_text, words) + _count(seg_text, words))

    def has_content(m: Meeting) -> bool:
        return m.id in transcripts or m.id in summaries or m.id in actions

    candidates = [m for m in searched if has_content(m)]
    if not candidates:
        return "", _Tags(), len(searched)
    newest_first = sorted(candidates, key=lambda m: meeting_when(m) or datetime.min, reverse=True)
    if meeting is not None:
        chosen = newest_first[:1]
    else:
        scored = [(score(m), m) for m in newest_first]
        relevant = [m for s, m in sorted(scored, key=lambda p: -p[0]) if s > 0][:MAX_MEETINGS]
        chosen = list(relevant)
        for m in newest_first:                              # general questions: the latest meetings
            if len(chosen) >= MIN_MEETINGS:
                break
            if m not in chosen:
                chosen.append(m)

    ctx = assignments.Context(db, user)
    owner_names = {e: n for e, n in db.query(User.email, User.name).filter(User.email.in_({m.owner_email for m in chosen if m.owner_email})) if n}

    def render(meetings: List[Meeting], n_segments: int, whole: bool) -> Tuple[str, _Tags]:
        tags = _Tags()
        blocks: List[str] = []
        for m in meetings:
            when = meeting_when(m)
            who = "you own it" if auth.is_owner(user, m) else f"shared by {owner_names.get(m.owner_email) or m.owner_email or 'someone'}"
            head = f"[{tags.tag(m)}] \"{clean(m.title, 120)}\" — {when.strftime('%d %b %Y') if when else 'no date'} ({who})"
            lines = [head]
            summary = _summary_text(summaries.get(m.id))
            if summary:
                lines.append(summary)
            acts = actions.get(m.id, [])
            if acts:
                lines.append("Action items:")
                lines += [_task_line(a, ctx.view(m, a)["mine"]) for a in acts[:25]]
            t = transcripts.get(m.id)
            if t and t.segments:
                full = _segment_lines(list(t.segments))
                if whole and sum(len(x) for x in full) <= WHOLE_TRANSCRIPT_CHARS:
                    lines += ["Transcript:"] + full
                elif n_segments:
                    lines += ["Transcript excerpts:"] + _segment_lines(_pick_segments(list(t.segments), words, n_segments))
            blocks.append("\n".join(lines))

        # the user's own open tasks anywhere: answers "what do I still have to do?"
        mine: List[str] = []
        by_id = {m.id: m for m in searched}
        for a in db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(ids), ActionItemDB.status != "completed"):
            if len(mine) >= MAX_OPEN_TASKS:
                break
            if ctx.view(by_id[a.meeting_id], a)["mine"]:
                mine.append(f"{_task_line(a, True)} — in meeting [{tags.tag(by_id[a.meeting_id])}]")
        if mine:
            blocks.append(f"{user.name or user.email}'s open tasks (assigned to them):\n" + "\n".join(mine))
        return "\n\n".join(blocks), tags

    # Shrink until it fits: first fewer transcript lines, then fewer meetings.
    text, tags = "", _Tags()
    for count in range(len(chosen), 0, -1):
        for n_segments, whole in ((SEGMENTS_PER_MEETING, meeting is not None), (4, False), (2, False), (0, False)):
            text, tags = render(chosen[:count], n_segments, whole)
            if len(text) <= max_chars:
                return text, tags, len(searched)
    return text[: max_chars - 1] + "…", tags, len(searched)


def history_text(history: List[Dict[str, str]]) -> str:
    """The last few turns, shortened, so follow-up questions make sense."""
    lines = []
    for turn in history[-3:]:
        lines.append(f"Q: {clean(turn.get('question'), 300)}")
        lines.append(f"A: {clean(turn.get('answer'), 500)}")
    return "\n".join(lines)


CITATION = re.compile(r"\[M(\d+)\]")


def sources_for(answer: str, tags: "_Tags") -> List[Dict[str, Any]]:
    """The meetings an answer cites (all that were searched, if it cites none)."""
    by_tag = {tag: tags.meetings[mid] for mid, tag in tags.by_meeting.items()}
    cited = []
    for n in CITATION.findall(answer or ""):
        tag = f"M{n}"
        if tag in by_tag and tag not in cited:
            cited.append(tag)
    cited_flag = bool(cited)
    order = cited or list(by_tag)[:MAX_MEETINGS]
    out = []
    for tag in order:
        m = by_tag[tag]
        when = meeting_when(m)
        out.append({"tag": tag, "meeting_id": m.id, "title": m.title,
                    "date": when.strftime("%Y-%m-%d") if when else None, "cited": cited_flag})
    return out
