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


def _sample_segments():
    from src.models.schemas import TranscriptSegment

    def seg(i, start, end, text, words=None):
        return TranscriptSegment(
            id=i, start_time=start, end_time=end, text=text, words=words
        )

    return [
        seg(0, 0.0, 5.0, "Welcome everyone to today's meeting."),
        seg(1, 5.0, 12.0, "Let's review the action items."),
        seg(2, 12.0, 18.0, "I completed the research."),
    ]


def test_transcribe_missing_file_raises(transcriber):
    with pytest.raises(FileNotFoundError):
        transcriber.transcribe("/nonexistent/audio.wav")


def test_transcribe_without_model_does_not_fake_output(transcriber, tmp_path):
    """No silent mock data when Whisper is unavailable."""
    from src.agent.transcriber import TranscriptionUnavailableError

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    transcriber.model = None
    with pytest.raises(TranscriptionUnavailableError):
        transcriber.transcribe(str(audio))


def test_logprob_to_confidence_is_probability():
    assert MeetingTranscriber._logprob_to_confidence(0.0) == 1.0
    assert 0.0 < MeetingTranscriber._logprob_to_confidence(-0.5) < 1.0
    assert MeetingTranscriber._logprob_to_confidence(None) == 0.0


def test_missing_hf_token_raises_diarization_error(transcriber, monkeypatch):
    from src.agent.transcriber import DiarizationUnavailableError

    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(DiarizationUnavailableError):
        transcriber._load_diarization_pipeline()


def test_timestamp_formatting(transcriber):
    """Test timestamp formatting"""
    srt_format = transcriber._format_timestamp(125.5, srt=True)
    vtt_format = transcriber._format_timestamp(125.5, srt=False)

    assert "00:02:05" in srt_format
    assert "," in srt_format  # SRT uses comma
    assert "." in vtt_format  # VTT uses period


def test_speaker_identification_assigns_labels(transcriber, monkeypatch):
    """Speaker labels should follow mocked diarization intervals."""
    from pyannote.core import Annotation, Segment

    segments = _sample_segments()

    annotation = Annotation()
    annotation[Segment(0.0, 12.0)] = "SPEAKER_A"
    annotation[Segment(12.0, 20.0)] = "SPEAKER_B"

    class MockPipeline:
        def __call__(self, audio_path):
            assert audio_path == "test.wav"
            return annotation

    monkeypatch.setattr(
        transcriber,
        "_load_diarization_pipeline",
        lambda: MockPipeline(),
    )

    result = transcriber._identify_speakers(segments, "test.wav")

    assert len(result) == 3
    assert [segment.speaker for segment in result] == [
        "Speaker 1",
        "Speaker 1",
        "Speaker 2",
    ]



def test_segment_spanning_speaker_change_is_split(transcriber, monkeypatch):
    """A segment crossing a speaker turn is split using word timings."""
    from pyannote.core import Annotation, Segment
    from src.models.schemas import TranscriptSegment

    words = [
        {"word": " Good", "start": 0.0, "end": 0.5},
        {"word": " morning", "start": 0.5, "end": 1.0},
        {"word": " thanks", "start": 2.1, "end": 2.6},
        {"word": " everyone", "start": 2.6, "end": 3.0},
    ]
    segment = TranscriptSegment(
        id=0, start_time=0.0, end_time=3.0,
        text="Good morning thanks everyone", words=words,
    )

    annotation = Annotation()
    annotation[Segment(0.0, 2.0)] = "A"
    annotation[Segment(2.0, 3.5)] = "B"

    class P:
        def __call__(self, path):
            return annotation

    monkeypatch.setattr(transcriber, "_load_diarization_pipeline", lambda: P())
    result = transcriber._identify_speakers([segment], "x.wav")

    assert [(r.speaker, r.text) for r in result] == [
        ("Speaker 1", "Good morning"),
        ("Speaker 2", "thanks everyone"),
    ]
    assert [r.id for r in result] == [0, 1]
    assert result[1].start_time == 2.1


def test_words_are_not_serialized():
    from src.models.schemas import TranscriptSegment

    seg = TranscriptSegment(
        id=0, start_time=0, end_time=1, text="hi",
        words=[{"word": "hi", "start": 0, "end": 1}],
    )
    assert "words" not in seg.dict()


# --- non-WAV audio is converted before speaker analysis (libsndfile cannot read it)

import os
import shutil
import subprocess

from src.agent.transcriber import DiarizationUnavailableError, readable_by_pyannote

_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_wav_and_flac_are_used_as_they_are():
    for name in ("a.wav", "A.WAV", "b.flac"):
        with readable_by_pyannote(name) as p:
            assert p == name


@_ffmpeg
def test_webm_is_converted_to_a_temporary_wav_and_cleaned_up(tmp_path):
    from tests.conftest import TEST_WAV
    webm = tmp_path / "rec.webm"
    with open(webm, "wb") as f:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", TEST_WAV, "-c:a", "libopus", "-f", "webm", "-"],
                       stdout=f, check=True)
    with readable_by_pyannote(str(webm)) as p:
        assert p.endswith(".wav") and p != str(webm) and os.path.getsize(p) > 1000
        import soundfile
        data, rate = soundfile.read(p)
        assert rate == 16000 and data.ndim == 1                       # mono, 16 kHz
        temp = p
    assert not os.path.exists(temp)                                    # removed afterwards
    assert os.path.exists(webm)                                        # original untouched


@_ffmpeg
def test_undecodable_audio_is_reported_not_crashed(tmp_path):
    bad = tmp_path / "junk.webm"
    bad.write_bytes(b"not audio at all")
    with pytest.raises(DiarizationUnavailableError):
        with readable_by_pyannote(str(bad)):
            pass


def test_speaker_analysis_receives_the_converted_file(transcriber, monkeypatch, tmp_path):
    """The pipeline must be handed the readable path, never the raw WebM."""
    from pyannote.core import Annotation, Segment
    seen = []
    ann = Annotation()
    ann[Segment(0, 12)] = "A"

    class P:
        def __call__(self, path, **kw):
            seen.append(path)
            return ann

    monkeypatch.setattr(transcriber, "_load_diarization_pipeline", lambda: P())
    import src.agent.transcriber as tr
    from contextlib import contextmanager

    @contextmanager
    def fake(path):
        yield "/converted.wav"

    monkeypatch.setattr(tr, "readable_by_pyannote", fake)
    transcriber._identify_speakers(_sample_segments(), "recording.webm")
    assert seen == ["/converted.wav"]


# ---------------------------------------- speaker analysis runs alongside Whisper

def _timed_transcriber(transcriber, monkeypatch, tmp_path, whisper_s, diarize_s, diarize_error=None):
    """Whisper and the speaker analysis each take a fixed time; returns the audio path."""
    import time
    from pyannote.core import Annotation, Segment

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    annotation = Annotation()
    annotation[Segment(0.0, 12.0)] = "A"
    annotation[Segment(12.0, 20.0)] = "B"

    def whisper(self_path, language, words):
        time.sleep(whisper_s)
        return _sample_segments()

    class P:
        def __call__(self, path):
            time.sleep(diarize_s)
            if diarize_error:
                raise diarize_error
            return annotation

    transcriber.model = object()
    monkeypatch.setattr("src.agent.transcriber.WHISPER_AVAILABLE", True)
    monkeypatch.setattr(transcriber, "_transcribe_with_whisper", whisper)
    monkeypatch.setattr(transcriber, "_load_diarization_pipeline", lambda: P())
    return str(audio)


def test_speakers_are_worked_out_while_whisper_runs(transcriber, monkeypatch, tmp_path):
    import time
    audio = _timed_transcriber(transcriber, monkeypatch, tmp_path, whisper_s=0.6, diarize_s=0.6)
    start = time.monotonic()
    result = transcriber.transcribe(audio, identify_speakers=True)
    elapsed = time.monotonic() - start
    assert elapsed < 1.1                                    # not 0.6 + 0.6
    assert [s.speaker for s in result.segments] == ["Speaker 1", "Speaker 1", "Speaker 2"]


def test_without_speaker_identification_nothing_extra_runs(transcriber, monkeypatch, tmp_path):
    audio = _timed_transcriber(transcriber, monkeypatch, tmp_path, whisper_s=0, diarize_s=5)
    monkeypatch.setattr(transcriber, "_diarize_file", lambda p: (_ for _ in ()).throw(AssertionError("must not run")))
    result = transcriber.transcribe(audio, identify_speakers=False)
    assert all(s.speaker is None for s in result.segments)


def test_a_speaker_analysis_failure_still_fails_the_transcription(transcriber, monkeypatch, tmp_path):
    from src.agent.transcriber import DiarizationUnavailableError
    audio = _timed_transcriber(transcriber, monkeypatch, tmp_path, whisper_s=0, diarize_s=0,
                               diarize_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        transcriber.transcribe(audio, identify_speakers=True)


def test_a_whisper_failure_is_reported_even_though_the_analysis_was_started(transcriber, monkeypatch, tmp_path):
    audio = _timed_transcriber(transcriber, monkeypatch, tmp_path, whisper_s=0, diarize_s=0.2)

    def broken(path, language, words):
        raise ValueError("no speech model")
    monkeypatch.setattr(transcriber, "_transcribe_with_whisper", broken)
    with pytest.raises(ValueError, match="no speech model"):
        transcriber.transcribe(audio, identify_speakers=True)


# ------------------------------------------------------- GPU for the speaker model

def test_the_speaker_model_device_can_be_forced(monkeypatch):
    for value in ("cpu", "mps", "cuda", " CPU "):
        monkeypatch.setenv("DIARIZATION_DEVICE", value)
        assert MeetingTranscriber._pick_device() == value.strip().lower()


def test_auto_uses_the_gpu_when_there_is_one_and_the_cpu_otherwise(monkeypatch):
    import torch
    monkeypatch.delenv("DIARIZATION_DEVICE", raising=False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    assert MeetingTranscriber._pick_device() == "mps"
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    assert MeetingTranscriber._pick_device() == "cpu"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert MeetingTranscriber._pick_device() == "cuda"


def test_a_gpu_that_cannot_be_used_falls_back_to_the_cpu(transcriber, monkeypatch):
    monkeypatch.setenv("DIARIZATION_DEVICE", "mps")

    class P:
        def to(self, device):
            raise RuntimeError("no metal here")
    assert transcriber._move_to_gpu(P()) == "cpu"


def test_a_gpu_that_works_is_used(transcriber, monkeypatch):
    monkeypatch.setenv("DIARIZATION_DEVICE", "mps")
    moved = []

    class P:
        def to(self, device):
            moved.append(str(device))
    assert transcriber._move_to_gpu(P()) == "mps" and moved == ["mps"]


def test_a_failure_on_the_gpu_is_retried_on_the_cpu(transcriber, monkeypatch, tmp_path):
    from pyannote.core import Annotation, Segment
    annotation = Annotation()
    annotation[Segment(0.0, 5.0)] = "A"
    calls, moves = [], []

    class P:
        def __call__(self, path, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("operator not implemented for MPS")
            return annotation

        def to(self, device):
            moves.append(str(device))

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcriber, "_load_diarization_pipeline", lambda: P())
    transcriber._pipeline_device = "mps"
    turns, _ = transcriber._diarize_file(str(audio))
    assert turns == [(0.0, 5.0, "A")] and moves == ["cpu"] and transcriber._pipeline_device == "cpu"


def test_a_failure_on_the_cpu_is_not_retried(transcriber, monkeypatch, tmp_path):
    class P:
        def __call__(self, path, **kw):
            raise RuntimeError("really broken")

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    monkeypatch.setattr(transcriber, "_load_diarization_pipeline", lambda: P())
    with pytest.raises(RuntimeError, match="really broken"):
        transcriber._diarize_file(str(audio))
