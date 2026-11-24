"""Audio upload validation and storage."""

import os
import re
import json
import shutil
import subprocess
import uuid
from typing import Optional

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".mp4"}


class AudioValidationError(ValueError):
    """Raised when an uploaded audio file is invalid."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


CONTAINER_ROOT = "/data/audio"      # where recordings are stored, as the Docker containers see it


def storage_root() -> str:
    return os.getenv("AUDIO_STORAGE_PATH", CONTAINER_ROOT)


def local_path(path: Optional[str]) -> Optional[str]:
    """
    The path to read a stored recording from. Paths are saved the way the containers see them
    (/data/audio/...); a worker running on the host sees the same folder at AUDIO_STORAGE_PATH.
    """
    root = storage_root().rstrip("/")
    if path and root != CONTAINER_ROOT and path.startswith(CONTAINER_ROOT + "/"):
        return root + path[len(CONTAINER_ROOT):]
    return path


def canonical_path(path: Optional[str]) -> Optional[str]:
    """The reverse of local_path: what is saved in the database, so it reads the same everywhere."""
    root = storage_root().rstrip("/")
    if path and root != CONTAINER_ROOT and path.startswith(root + "/"):
        return CONTAINER_ROOT + path[len(root):]
    return path


def max_audio_bytes() -> int:
    try:
        mb = float(os.getenv("MAX_AUDIO_SIZE_MB", "200"))
    except ValueError:
        mb = 200.0
    return int(mb * 1024 * 1024)


def sanitize_filename(filename: str | None) -> str:
    name = os.path.basename((filename or "audio").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "audio"
    return name[:120]


def validate_extension(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AudioValidationError(
            "Unsupported audio type. Allowed: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS)),
            status_code=415,
        )
    return ext


async def save_upload(upload, meeting_id: str, ext: str, limit: int | None = None) -> tuple[str, int]:
    """Stream an UploadFile to storage, enforcing the size limit (the default limit, unless one is given)."""
    directory = os.path.join(storage_root(), meeting_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{uuid.uuid4()}{ext}")
    limit = limit or max_audio_bytes()
    size = 0

    try:
        with open(path, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise AudioValidationError(
                        f"Audio file exceeds the {limit / (1024 * 1024):g} MB limit",
                        status_code=413,
                    )
                out.write(chunk)
        if size == 0:
            raise AudioValidationError("Uploaded audio file is empty")
    except Exception:
        remove_stored(path, meeting_id)
        raise
    return path, size


def probe_duration(path: str) -> float:
    """Confirm the file is decodable audio and return its duration (s)."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioValidationError(
            "Audio inspection is unavailable", status_code=503
        ) from exc

    if proc.returncode != 0:
        raise AudioValidationError("File is not valid, decodable audio")
    try:
        info = json.loads(proc.stdout)
        if not any(
            s.get("codec_type") == "audio" for s in info.get("streams", [])
        ):
            raise AudioValidationError("File contains no audio stream")
        duration = _as_seconds(info["format"].get("duration"))
        if duration <= 0:
            for stream in info.get("streams", []):
                duration = max(duration, _as_seconds(stream.get("duration")))
    except (ValueError, KeyError):
        raise AudioValidationError("File is not valid, decodable audio")
    if duration <= 0:
        # Browser recordings (MediaRecorder WebM) carry no length in their header:
        # measure it by decoding.
        duration = _decoded_duration(path)
    return duration


def _as_seconds(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0                      # "N/A", missing, ...
    return seconds if seconds > 0 and seconds != float("inf") else 0.0


_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _decoded_duration(path: str) -> float:
    """Length of the audio found by decoding it (0.0 if ffmpeg cannot tell)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-f", "null",
             "-progress", "pipe:1", "-"],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    # -progress prints out_time_us=<microseconds> repeatedly; the last one is the total.
    last = 0
    for line in proc.stdout.splitlines():
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                last = int(line.split("=", 1)[1])
            except ValueError:
                pass
    return last / 1_000_000 if last > 0 else 0.0


def remove_stored(path: str | None, meeting_id: str | None = None) -> None:
    """Delete a stored file (and its meeting directory if left empty)."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
        directory = os.path.dirname(path)
        if (
            directory.startswith(storage_root())
            and os.path.isdir(directory)
            and not os.listdir(directory)
        ):
            shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        pass
