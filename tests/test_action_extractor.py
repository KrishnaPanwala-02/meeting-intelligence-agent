"""Tests for ActionItemExtractor: extraction and deadline resolution."""

import json
from datetime import date, datetime

import pytest

from src.agent.action_extractor import (
    ActionItemExtractor,
    ActionExtractionError,
)
from src.models.schemas import TranscriptionResult, TranscriptSegment

REF = date(2026, 10, 28)  # Wednesday (the real test meeting's date)


@pytest.fixture
def ex():
    return ActionItemExtractor(ollama_host="http://x")


def D(y, m, d):
    return datetime(y, m, d)


@pytest.mark.parametrize("phrase,expected", [
    # Previously supported behaviour (must not change)
    ("by Friday", D(2026, 10, 30)),
    ("by Monday", D(2026, 11, 2)),
    ("Tomorrow", D(2026, 10, 29)),
    ("today", D(2026, 10, 28)),
    ("next Friday", D(2026, 11, 6)),
    ("November 5, 2026", D(2026, 11, 5)),
    ("2026-11-05", D(2026, 11, 5)),
    # New coverage
    ("this Friday", D(2026, 10, 30)),
    ("by next week", D(2026, 11, 6)),
    ("end of the week", D(2026, 10, 30)),
    ("end of the month", D(2026, 10, 31)),
    ("by end of day", D(2026, 10, 28)),
    ("day after tomorrow", D(2026, 10, 30)),
    ("within three days", D(2026, 10, 31)),
    ("in 2 weeks", D(2026, 11, 11)),
    ("a week from now", D(2026, 11, 4)),
    ("On 15 November", D(2026, 11, 15)),
    ("by November 15th", D(2026, 11, 15)),
    ("15th of November 2027", D(2027, 11, 15)),
    ("by 10 October", D(2027, 10, 10)),  # already passed -> next year
])
def test_resolves_supported_phrases(ex, phrase, expected):
    assert ex._resolve_due_date(phrase, REF) == expected


@pytest.mark.parametrize("phrase", [
    "soon", "later", "asap", "next month", "sometime", "31 February",
    "in a few days", "within forever", "", None,
])
def test_ambiguous_phrases_are_not_guessed(ex, phrase):
    assert ex._resolve_due_date(phrase, REF) is None


def test_no_reference_date_means_unresolved(ex):
    assert ex._resolve_due_date("by Friday", None) is None


def test_end_of_week_after_friday_is_unresolved(ex):
    saturday = date(2026, 10, 31)
    assert ex._resolve_due_date("end of the week", saturday) is None


def make_transcript(lines):
    segs = [
        TranscriptSegment(id=i, start_time=i, end_time=i + 1,
                          text=t, speaker=sp)
        for i, (sp, t) in enumerate(lines)
    ]
    return TranscriptionResult(
        audio_file="", language="en", segments=segs, duration_seconds=6,
        word_count=19, speaker_count=2,
        created_at=datetime(2026, 11, 11, 9, 0),  # NOT the meeting date
    )


AI_REPLY = json.dumps([
    {"task": "Complete the model training", "assignee": "Speaker 1",
     "due_date_phrase": "by Friday", "priority": "high"},
    {"task": "Prepare the project report", "assignee": "Speaker 2",
     "due_date_phrase": "by Monday", "priority": "medium"},
    {"task": "complete the model training!", "assignee": "Speaker 1",
     "due_date_phrase": "by Friday", "priority": "high"},
    {"task": "Review budget", "assignee": "Speaker 1",
     "due_date_phrase": "soon", "priority": "low"},
])


@pytest.mark.asyncio
async def test_extract_uses_meeting_reference_date(ex, monkeypatch):
    async def fake(prompt):
        return AI_REPLY
    monkeypatch.setattr(ex, "_call_ollama", fake)

    t = make_transcript([("Speaker 1", "I will complete it by Friday.")])
    items = await ex.extract(t, reference_date=REF)

    by_task = {i.task: i for i in items}
    assert len(items) == 3                                   # duplicate gone
    assert by_task["Complete the model training"].due_date == D(2026, 10, 30)
    assert by_task["Prepare the project report"].due_date == D(2026, 11, 2)
    unresolved = by_task["Review budget"]
    assert unresolved.due_date is None
    assert unresolved.due_date_phrase == "soon"              # kept for review


@pytest.mark.asyncio
async def test_extract_defaults_to_transcript_date(ex, monkeypatch):
    async def fake(prompt):
        return AI_REPLY
    monkeypatch.setattr(ex, "_call_ollama", fake)
    t = make_transcript([("Speaker 1", "x")])
    items = await ex.extract(t)  # 2026-11-11 is a Wednesday
    friday = [i for i in items if i.task.startswith("Complete")][0]
    assert friday.due_date == D(2026, 11, 13)


@pytest.mark.asyncio
async def test_ollama_failure_raises(monkeypatch):
    ex = ActionItemExtractor(ollama_host="http://127.0.0.1:1")
    t = make_transcript([("Speaker 1", "I will do it.")])
    with pytest.raises(ActionExtractionError):
        await ex.extract(t)


@pytest.mark.asyncio
async def test_long_transcript_is_chunked_and_deduplicated(ex, monkeypatch):
    prompts = []

    async def fake(prompt):
        prompts.append(prompt)
        return json.dumps([{"task": "Ship it", "assignee": "TBD",
                            "due_date_phrase": None, "priority": "low"}])
    monkeypatch.setattr(ex, "_call_ollama", fake)

    lines = [("Speaker 1", "word " * 100) for _ in range(60)]
    items = await ex.extract(make_transcript(lines))
    assert len(prompts) > 1
    assert len(items) == 1


@pytest.mark.asyncio
async def test_no_action_items_returns_empty(ex, monkeypatch):
    async def fake(prompt):
        return "[]"
    monkeypatch.setattr(ex, "_call_ollama", fake)
    assert await ex.extract(make_transcript([("Speaker 1", "Hello.")])) == []


def test_a_cut_off_answer_keeps_the_complete_items():
    from src.agent.action_extractor import ActionItemExtractor
    e = ActionItemExtractor.__new__(ActionItemExtractor)
    cut = '[{"task":"Do A","assignee":"Jai","due_date_phrase":null,"priority":"high"},{"task":"Do B","assignee":"Ne'
    assert [i.task for i in e._parse_action_items(cut)] == ["Do A"]
