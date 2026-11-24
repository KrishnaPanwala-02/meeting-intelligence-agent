"""Upload validation and storage (real ffprobe, real audio)."""

import io
import os

import pytest
from fastapi import UploadFile

from src.services import audio_storage as a
from tests.conftest import TEST_WAV


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path / "audio"))
    return tmp_path / "audio"


def upload(data: bytes, name="a.wav"):
    return UploadFile(file=io.BytesIO(data), filename=name)


@pytest.mark.parametrize("raw,expected", [
    ("meeting.wav", "meeting.wav"),
    ("../../etc/passwd.wav", "passwd.wav"),
    ("..\\..\\windows\\evil.wav", "evil.wav"),
    ("my file (1).wav", "my_file__1_.wav"),
    ("", "audio"), (None, "audio"), ("...", "audio"),
    ("x" * 500 + ".wav", ("x" * 117)[:117]),
])
def test_sanitize_filename(raw, expected):
    out = a.sanitize_filename(raw)
    assert "/" not in out and "\\" not in out and ".." not in out and len(out) <= 120
    if len(expected) < 100:
        assert out == expected


@pytest.mark.parametrize("name", ["a.wav", "A.MP3", "b.m4a", "c.ogg", "d.flac", "e.webm", "f.mp4"])
def test_allowed_extensions(name):
    assert a.validate_extension(name) == os.path.splitext(name)[1].lower()


@pytest.mark.parametrize("name", ["a.txt", "a.exe", "a.wav.exe", "a", "a.py", ".wav.php"])
def test_rejected_extensions(name):
    with pytest.raises(a.AudioValidationError) as e:
        a.validate_extension(name)
    assert e.value.status_code == 415


@pytest.mark.asyncio
async def test_save_upload_streams_to_meeting_directory(storage):
    data = open(TEST_WAV, "rb").read()
    path, size = await a.save_upload(upload(data), "meeting-1", ".wav")
    assert size == len(data) and open(path, "rb").read() == data
    assert path.startswith(str(storage / "meeting-1")) and path.endswith(".wav")


@pytest.mark.asyncio
async def test_empty_upload_rejected_and_nothing_left_behind(storage):
    with pytest.raises(a.AudioValidationError) as e:
        await a.save_upload(upload(b""), "m", ".wav")
    assert e.value.status_code == 400
    assert not (storage / "m").exists()


@pytest.mark.asyncio
async def test_oversize_upload_rejected_and_cleaned_up(storage, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SIZE_MB", "0.001")           # ~1 KB
    with pytest.raises(a.AudioValidationError) as e:
        await a.save_upload(upload(b"x" * 5000), "m", ".wav")
    assert e.value.status_code == 413 and "KB" not in str(e.value)
    assert not (storage / "m").exists()


def test_size_limit_env_parsing(monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SIZE_MB", "2")
    assert a.max_audio_bytes() == 2 * 1024 * 1024
    monkeypatch.setenv("MAX_AUDIO_SIZE_MB", "not-a-number")
    assert a.max_audio_bytes() == 200 * 1024 * 1024
    monkeypatch.delenv("MAX_AUDIO_SIZE_MB")
    assert a.max_audio_bytes() == 200 * 1024 * 1024


def test_probe_real_audio_duration():
    assert a.probe_duration(TEST_WAV) == pytest.approx(6.3, abs=0.2)


def test_probe_rejects_non_audio_and_corrupt(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(os.urandom(5000))
    with pytest.raises(a.AudioValidationError):
        a.probe_duration(str(bad))
    txt = tmp_path / "t.wav"
    txt.write_text("hello")
    with pytest.raises(a.AudioValidationError):
        a.probe_duration(str(txt))


def test_probe_without_ffprobe_is_a_503(monkeypatch):
    monkeypatch.setattr(a.subprocess, "run", lambda *x, **k: (_ for _ in ()).throw(OSError("no ffprobe")))
    with pytest.raises(a.AudioValidationError) as e:
        a.probe_duration(TEST_WAV)
    assert e.value.status_code == 503


def test_remove_stored_deletes_file_and_empty_directory(storage):
    d = storage / "m"
    d.mkdir(parents=True)
    f = d / "x.wav"
    f.write_bytes(b"1")
    a.remove_stored(str(f), "m")
    assert not f.exists() and not d.exists()
    a.remove_stored(None)                       # no-op
    a.remove_stored(str(storage / "missing.wav"))


def test_remove_stored_keeps_directory_with_other_files(storage):
    d = storage / "m"
    d.mkdir(parents=True)
    (d / "keep.wav").write_bytes(b"1")
    (d / "drop.wav").write_bytes(b"1")
    a.remove_stored(str(d / "drop.wav"), "m")
    assert (d / "keep.wav").exists()


# --- browser recordings: MediaRecorder WebM has no duration in its header

import shutil
import subprocess

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def streamed(tmp_path, codec, fmt, name):
    """Encode like a browser does: written to a pipe, so no length is stored."""
    out = tmp_path / name
    with open(out, "wb") as f:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", TEST_WAV, "-c:a", codec, "-f", fmt, "-"],
                       stdout=f, check=True)
    return str(out)


@needs_ffmpeg
def test_probe_measures_a_streamed_webm_with_no_stored_duration(tmp_path):
    path = streamed(tmp_path, "libopus", "webm", "rec.webm")
    assert a.probe_duration(path) == pytest.approx(6.3, abs=0.3)


@needs_ffmpeg
def test_probe_measures_a_fragmented_mp4(tmp_path):
    out = tmp_path / "rec.mp4"
    with open(out, "wb") as f:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", TEST_WAV, "-c:a", "aac", "-f", "mp4",
                        "-movflags", "frag_keyframe+empty_moov", "-"], stdout=f, check=True)
    assert a.probe_duration(str(out)) == pytest.approx(6.3, abs=0.3)


def test_as_seconds_ignores_missing_and_nonsense_values():
    for bad in (None, "N/A", "", "abc", 0, -1, "inf", float("inf")):
        assert a._as_seconds(bad) == 0.0
    assert a._as_seconds("6.5") == 6.5


# ----------------------------------------- one recordings folder, seen from two places

def test_paths_are_unchanged_where_the_folder_is_at_its_usual_place(monkeypatch):
    monkeypatch.delenv("AUDIO_STORAGE_PATH", raising=False)
    p = "/data/audio/m1/a.wav"
    assert a.local_path(p) == p and a.canonical_path(p) == p


def test_a_host_worker_reads_the_same_folder_from_where_it_is_on_the_host(monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", "/Users/me/proj/data/audio")
    assert a.local_path("/data/audio/m1/a.wav") == "/Users/me/proj/data/audio/m1/a.wav"
    assert a.canonical_path("/Users/me/proj/data/audio/m1/a.wav") == "/data/audio/m1/a.wav"
    # already a host path, an unrelated path, and nothing at all are left alone
    assert a.local_path("/Users/me/proj/data/audio/m1/a.wav") == "/Users/me/proj/data/audio/m1/a.wav"
    assert a.local_path("/tmp/x.wav") == "/tmp/x.wav"
    assert a.local_path(None) is None and a.canonical_path("") == ""


def test_a_similar_folder_name_is_not_mistaken_for_the_recordings_folder(monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_PATH", "/Users/me/data/audio")
    assert a.local_path("/data/audio-old/a.wav") == "/data/audio-old/a.wav"
    assert a.canonical_path("/Users/me/data/audio2/a.wav") == "/Users/me/data/audio2/a.wav"
