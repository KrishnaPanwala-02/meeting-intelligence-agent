"""Tests for MeetingSummarizer (Ollama mocked)."""

import json
from datetime import datetime

import pytest

from src.agent.summarizer import (
    MeetingSummarizer,
    SummarizationError,
    MAX_DIRECT_CHARS,
)
from src.models.schemas import TranscriptionResult, TranscriptSegment


def make_transcript(texts, speaker="Speaker 1"):
    segs = [
        TranscriptSegment(
            id=i, start_time=i, end_time=i + 1, text=t, speaker=speaker
        )
        for i, t in enumerate(texts)
    ]
    return TranscriptionResult(
        audio_file="", language="en", segments=segs,
        duration_seconds=130.0, word_count=10, speaker_count=1,
        created_at=datetime.now(),
    )


STRUCTURED = {
    "key_points": [
        {"topic": "Training", "description": "Model training status"},
        {"topic": "Training", "description": "Model training status"},
    ],
    "decisions": [{"decision": "Ship v2", "owner": "", "context": ""}],
    "next_steps": [
        "Here are the next steps:",
        "Alice finishes the report",
        "alice finishes the report",
    ],
    "key_quotes": [
        "We will ship version two on Friday",
        "A sentence that was never spoken here",
    ],
}


@pytest.fixture
def summarizer(monkeypatch):
    s = MeetingSummarizer(ollama_host="http://x")
    calls = []

    async def fake(prompt, schema=None, num_predict=800):
        calls.append(prompt)
        return json.dumps(STRUCTURED) if schema else "A short summary."

    monkeypatch.setattr(s, "_call_ollama", fake)
    s.calls = calls
    return s


@pytest.mark.asyncio
async def test_summary_is_built_from_transcript_and_cleaned(summarizer):
    t = make_transcript(["We will ship version two on Friday, agreed."])
    result = await summarizer.summarize(t, "Release sync", ["a@x.com"])

    assert result.executive_summary == "A short summary."
    assert "[Speaker 1] We will ship version two" in summarizer.calls[0]
    assert len(result.key_points) == 1            # duplicate removed
    assert result.decisions[0].owner == "TBD"     # blank owner defaulted
    assert result.next_steps == ["Alice finishes the report"]
    assert result.key_quotes == ["We will ship version two on Friday"]
    assert result.duration_minutes == 3
    assert result.participants == ["a@x.com"]


@pytest.mark.asyncio
async def test_empty_transcript_rejected(summarizer):
    with pytest.raises(SummarizationError):
        await summarizer.summarize(make_transcript(["  "]))


@pytest.mark.asyncio
async def test_long_transcript_is_condensed_not_truncated(monkeypatch):
    s = MeetingSummarizer(ollama_host="http://x")
    prompts = []

    async def fake(prompt, schema=None, num_predict=800):
        prompts.append(prompt)
        return json.dumps(STRUCTURED) if schema else "notes"

    monkeypatch.setattr(s, "_call_ollama", fake)
    lines = [f"Sentence number {i} about topic {i}." + " pad" * 20
             for i in range(400)]
    assert sum(len(x) for x in lines) > MAX_DIRECT_CHARS
    await s.summarize(make_transcript(lines))

    condense = [p for p in prompts if p.startswith("Condense")]
    assert len(condense) >= 2
    assert "Sentence number 399" in "".join(condense)  # tail not dropped


@pytest.mark.asyncio
async def test_ollama_failure_raises_instead_of_fake_summary():
    s = MeetingSummarizer(ollama_host="http://127.0.0.1:1", timeout=2)
    with pytest.raises(SummarizationError):
        await s.summarize(make_transcript(["Hello there everyone."]))


@pytest.mark.asyncio
async def test_malformed_structured_output(monkeypatch):
    s = MeetingSummarizer(ollama_host="http://x")

    async def fake(prompt, schema=None, num_predict=800):
        return "not json" if schema else "ok"

    monkeypatch.setattr(s, "_call_ollama", fake)
    with pytest.raises(SummarizationError):
        await s.summarize(make_transcript(["Hello there everyone."]))


@pytest.mark.asyncio
async def test_refusal_is_retried_then_rejected(monkeypatch):
    s = MeetingSummarizer(ollama_host="http://x")
    n = {"c": 0}

    async def fake(prompt, schema=None, num_predict=800):
        n["c"] += 1
        return "No summary can be provided." if not schema else "{}"

    monkeypatch.setattr(s, "_call_ollama", fake)
    with pytest.raises(SummarizationError):
        await s.summarize(make_transcript(["Hello there everyone."]))
    assert n["c"] == 2
