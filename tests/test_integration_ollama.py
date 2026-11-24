"""Integration tests against the real, locally hosted Ollama model.

Skipped automatically if Ollama (or the configured model) is unavailable.
Assertions are deliberately about structure and grounding, not exact wording,
because LLM output varies.
"""

import os
import re
from datetime import date, datetime

import httpx
import pytest

from src.agent.action_extractor import ActionItemExtractor
from src.agent.summarizer import MeetingSummarizer
from src.models.schemas import TranscriptionResult, TranscriptSegment

pytestmark = pytest.mark.integration

HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


@pytest.fixture(scope="module", autouse=True)
def ollama_available():
    try:
        tags = httpx.get(f"{HOST}/api/tags", timeout=3).json()
        names = {m["name"].split(":")[0] for m in tags.get("models", [])}
    except Exception:
        pytest.skip("Ollama is not reachable")
    if MODEL.split(":")[0] not in names:
        pytest.skip(f"Model {MODEL} is not installed")


LINES = [
    ("Speaker 1", "Good morning everyone. Today we need to plan the model release."),
    ("Speaker 2", "I think the accuracy is good enough, so I will write the release notes by Friday."),
    ("Speaker 1", "Great. I will complete the model training by Monday and then run the final evaluation."),
    ("Speaker 2", "We decided to ship version two without the experimental feature."),
    ("Speaker 1", "Agreed. Anna will review the budget, but there is no deadline for that yet."),
    ("Speaker 2", "Fine. Thanks everyone, that is all for today."),
]


def transcript():
    segs = [TranscriptSegment(id=i, start_time=i * 5, end_time=i * 5 + 4, text=t, speaker=s)
            for i, (s, t) in enumerate(LINES)]
    return TranscriptionResult(audio_file="", language="en", segments=segs, duration_seconds=30,
                               word_count=sum(len(t.split()) for _, t in LINES), speaker_count=2,
                               created_at=datetime(2026, 11, 11, 9, 0))


@pytest.mark.asyncio
async def test_real_summary_is_structured_and_grounded():
    s = MeetingSummarizer(ollama_host=HOST, model=MODEL)
    r = await s.summarize(transcript(), "Release planning", ["a@x.com"])
    assert len(r.executive_summary.split()) >= 8
    assert not re.match(r"(no summary|i cannot|unable)", r.executive_summary, re.I)
    assert r.key_points and all(p.description for p in r.key_points)
    assert r.duration_minutes == 1 and r.participants == ["a@x.com"]
    # no model chatter leaked into the lists
    for step in r.next_steps:
        assert not re.match(r"(here (are|is)|please note)", step, re.I)
    # any quote returned really appears in the transcript
    flat = re.sub(r"\W+", " ", " ".join(t for _, t in LINES)).lower()
    for q in r.key_quotes:
        assert re.sub(r"\W+", " ", q).lower().strip() in flat


@pytest.mark.asyncio
async def test_real_action_extraction_resolves_deadlines_in_python():
    ex = ActionItemExtractor(ollama_host=HOST, model=MODEL)
    items = await ex.extract(transcript(), reference_date=date(2026, 10, 28))   # a Wednesday
    assert len(items) >= 2
    tasks = " | ".join(i.task.lower() for i in items)
    assert "release notes" in tasks and "model training" in tasks
    assert {"Speaker 1", "Speaker 2"} <= {i.assignee for i in items}
    for i in items:
        phrase = (i.due_date_phrase or "").lower()
        if "friday" in phrase:
            assert i.due_date == datetime(2026, 10, 30)         # the LLM never computes dates
        if "monday" in phrase:
            assert i.due_date == datetime(2026, 11, 2)
        if i.due_date is not None:
            assert i.due_date.date() >= date(2026, 10, 28)
    # an unstated deadline must not be invented
    anna = [i for i in items if "budget" in i.task.lower()]
    assert all(i.due_date is None for i in anna)
