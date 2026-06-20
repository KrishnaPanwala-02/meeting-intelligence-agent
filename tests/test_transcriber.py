"""Tests for Meeting Transcriber"""

import pytest
from src.agent.transcriber import MeetingTranscriber


@pytest.fixture
def transcriber():
    """Create transcriber instance"""
    return MeetingTranscriber(model="base")


def test_transcriber_initialization(transcriber):
    """Test transcriber initializes correctly"""
    assert transcriber is not None
    assert transcriber.model_name == "base"


@pytest.mark.asyncio
async def test_mock_transcription(transcriber):
    """Test mock transcription"""
    # Use mock since actual audio files may not exist
    segments = transcriber._mock_transcription("test.wav")

    assert len(segments) > 0
    assert all(hasattr(seg, 'text') for seg in segments)
    assert all(hasattr(seg, 'start_time') for seg in segments)


def test_timestamp_formatting(transcriber):
    """Test timestamp formatting"""
    srt_format = transcriber._format_timestamp(125.5, srt=True)
    vtt_format = transcriber._format_timestamp(125.5, srt=False)

    assert "00:02:05" in srt_format
    assert "," in srt_format  # SRT uses comma
    assert "." in vtt_format  # VTT uses period
