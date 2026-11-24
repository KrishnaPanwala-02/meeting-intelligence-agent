"""Integration tests with a real recording and the real Whisper / Pyannote
models. Skipped when the models cannot be loaded (e.g. no network/cache)."""

import os

import pytest

from src.agent.transcriber import MeetingTranscriber
from tests.conftest import TEST_WAV

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def transcriber():
    t = MeetingTranscriber(model=os.getenv("WHISPER_MODEL", "base"))
    if t.model is None:
        pytest.skip("Whisper model unavailable")
    return t


def test_real_transcription_text_and_timestamps(transcriber):
    r = transcriber.transcribe(TEST_WAV, language="en")
    text = " ".join(s.text for s in r.segments).lower()
    assert "model training" in text and "project report" in text
    assert len(r.segments) >= 2 and r.word_count >= 15
    starts = [s.start_time for s in r.segments]
    assert starts == sorted(starts)
    assert all(s.end_time > s.start_time for s in r.segments)
    assert r.duration_seconds == pytest.approx(6.0, abs=0.7)       # audio length, not processing time
    assert r.processing_seconds is not None
    assert all(0.0 <= s.confidence <= 1.0 for s in r.segments)
    assert r.speaker_count == 0                                    # diarization is opt-in


def test_real_diarization_separates_the_two_speakers(transcriber):
    if not os.getenv("HF_TOKEN"):
        pytest.skip("HF_TOKEN not configured")
    r = transcriber.transcribe(TEST_WAV, language="en", identify_speakers=True)
    assert r.speaker_count == 2
    by = [(s.speaker, s.text.lower()) for s in r.segments]
    assert by[0][0] == "Speaker 1" and "model training" in by[0][1]
    assert by[-1][0] == "Speaker 2" and "project report" in by[-1][1]
    assert all(s.speaker for s in r.segments)


def test_real_transcription_of_silence_or_noise_does_not_invent_a_meeting(transcriber, tmp_path):
    import subprocess
    quiet = tmp_path / "silence.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "3", str(quiet)], check=True)
    r = transcriber.transcribe(str(quiet), language="en")
    assert r.word_count == 0 and r.segments == []
