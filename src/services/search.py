"""
Search across everything a person may read: meeting titles, summaries, tasks and
transcripts. All words of the query must appear in the same piece of text
(a title, a summary, a task, one transcript line); "double quotes" match a phrase.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.database import ActionItemDB, Meeting, Summary, Transcript, User
from . import auth
from .ask import clean, meeting_when

MAX_QUERY = 200
MAX_MEETINGS = 30
HITS_PER_MEETING = 6
SNIPPET_BEFORE, SNIPPET_AFTER = 60, 110
WEIGHT = {"title": 5, "task": 3, "summary": 2, "transcript": 1}


def parse_query(q: str) -> List[str]:
    """Lower-cased terms: each "quoted phrase" is one term, other words are single terms."""
    q = " ".join((q or "").split())[:MAX_QUERY]
    terms: List[str] = []
    for phrase in re.findall(r'"([^"]+)"', q):
        p = " ".join(phrase.split()).lower()
        if p:
            terms.append(p)
    rest = re.sub(r'"[^"]+"', " ", q).replace('"', " ")       # a stray quote is just punctuation
    for word in rest.lower().split():
        word = word.strip(".,;:!?()[]{}'’")
        if len(word) >= 2:
            terms.append(word)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def matches(text: Optional[str], terms: List[str]) -> bool:
    low = (text or "").lower()
    return bool(terms) and all(t in low for t in terms)


def snippet(text: str, terms: List[str]) -> str:
    """A short piece of the text around the first match."""
    text = " ".join((text or "").split())
    low = text.lower()
    pos = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
    start = max(0, pos - SNIPPET_BEFORE)
    end = min(len(text), pos + SNIPPET_AFTER)
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _summary_pieces(s: Summary) -> List[str]:
    pieces = [s.executive_summary or ""]
    for kp in s.key_points or []:
        if isinstance(kp, dict):
            pieces.append(f"{kp.get('topic', '')}: {kp.get('description', '')}")
    for d in s.decisions or []:
        if isinstance(d, dict):
            pieces.append(f"{d.get('decision', '')} ({d.get('owner', '')})")
    pieces += [str(x) for x in (s.next_steps or [])]
    return [p for p in pieces if p.strip()]


def search(db: Session, user: User, q: str, max_meetings: int = MAX_MEETINGS,
           hits_per_meeting: int = HITS_PER_MEETING) -> Dict[str, Any]:
    terms = parse_query(q)
    if not terms:
        return {"query": q, "terms": [], "total": 0, "meetings": []}

    meetings = [m for m in db.query(Meeting).all() if auth.can_read(user, m)]
    ids = [m.id for m in meetings]
    transcripts: Dict[str, Transcript] = {}
    for t in db.query(Transcript).filter(Transcript.meeting_id.in_(ids)).order_by(Transcript.created_at):
        transcripts[t.meeting_id] = t                    # latest wins
    summaries: Dict[str, Summary] = {}
    for s in db.query(Summary).filter(Summary.meeting_id.in_(ids)).order_by(Summary.created_at):
        summaries[s.meeting_id] = s
    actions: Dict[str, List[ActionItemDB]] = {}
    for a in db.query(ActionItemDB).filter(ActionItemDB.meeting_id.in_(ids)):
        actions.setdefault(a.meeting_id, []).append(a)

    results = []
    total = 0
    for m in meetings:
        hits: List[Dict[str, Any]] = []
        if matches(m.title, terms):
            hits.append({"kind": "title", "text": clean(m.title, 200)})
        s = summaries.get(m.id)
        for piece in (_summary_pieces(s) if s else []):
            if matches(piece, terms):
                hits.append({"kind": "summary", "text": snippet(clean(piece, 2000), terms)})
        for a in actions.get(m.id, []):
            if matches(f"{a.task} {a.assignee or ''}", terms):
                hits.append({"kind": "task", "text": clean(a.task, 300), "assignee": a.assignee,
                             "status": a.status,
                             "due": a.due_date.strftime("%Y-%m-%d") if a.due_date else None})
        t = transcripts.get(m.id)
        for seg in (t.segments if t else None) or []:
            if matches(f"{seg.get('speaker') or ''} {seg.get('text') or ''}", terms):
                hits.append({"kind": "transcript", "text": snippet(clean(seg.get("text"), 1500), terms),
                             "speaker": seg.get("speaker"), "segment_id": seg.get("id"),
                             "time": float(seg.get("start_time") or 0)})
        if not hits:
            continue
        total += len(hits)
        hits.sort(key=lambda h: -WEIGHT[h["kind"]])      # stable: transcript lines stay in time order
        when = meeting_when(m)
        results.append({
            "meeting_id": m.id, "title": m.title,
            "date": when.strftime("%Y-%m-%d") if when else None,
            "hit_count": len(hits), "hits": hits[:hits_per_meeting],
            "_rank": (sum(WEIGHT[h["kind"]] for h in hits), when or datetime.min),
        })

    results.sort(key=lambda r: r["_rank"], reverse=True)
    for r in results:
        del r["_rank"]
    return {"query": q, "terms": terms, "total": total, "meetings": results[:max_meetings]}
