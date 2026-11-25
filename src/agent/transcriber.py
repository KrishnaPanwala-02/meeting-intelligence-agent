"""
Meeting Transcription Agent
Real-time and batch transcription using Whisper AI.
"""

import os
import re
import math
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

try:
    import whisper
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    logger.error("Whisper is not installed - transcription is unavailable")
    WHISPER_AVAILABLE = False

from ..models.schemas import TranscriptSegment, TranscriptionResult


# The raw speaker label for the person using this computer in a recording that keeps the microphone
# (left channel) apart from the meeting tab (right channel).
LOCAL = "local"


class TranscriptionUnavailableError(RuntimeError):
    """Whisper is not installed or its model failed to load."""


class NoSpeechError(ValueError):
    """A voice sample did not contain a usable voice."""


class DiarizationUnavailableError(RuntimeError):
    """Speaker diarization cannot run (missing token, model, or access)."""


# Pyannote reads audio with libsndfile, which cannot open WebM / MP4 / M4A / MP3
# (browser recordings and phone files): those are converted to a temporary WAV.
_NATIVE_FOR_PYANNOTE = {".wav", ".flac"}


@contextmanager
def readable_by_pyannote(audio_path: str):
    """Yield a path Pyannote can open (the original, or a temporary 16 kHz mono WAV)."""
    if os.path.splitext(audio_path)[1].lower() in _NATIVE_FOR_PYANNOTE:
        yield audio_path
        return
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", audio_path,
                 "-vn", "-ac", "1", "-ar", "16000", tmp],
                capture_output=True, text=True, timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DiarizationUnavailableError(
                "Could not convert the audio for speaker analysis"
            ) from exc
        if proc.returncode != 0:
            raise DiarizationUnavailableError(
                "Could not decode the audio for speaker analysis"
            )
        yield tmp
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class MeetingTranscriber:
    """
    Meeting Transcription Agent

    Features:
    - Real-time transcription with Whisper
    - Batch transcription for recorded meetings
    - Speaker diarization
    - Multi-language support (99+ languages)
    - Multiple output formats (TXT, SRT, VTT)
    """

    def __init__(
        self,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8"
    ):
        """
        Initialize Meeting Transcriber

        Args:
            model: Whisper model size (tiny, base, small, medium, large-v3)
            device: Device to use (cpu, cuda)
            compute_type: Compute type for faster-whisper (int8, float16, float32)
        """
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.model = None
        self.diarization_pipeline = None
        self._pipeline_device = "cpu"
        self.last_speaker_voices: Optional[Dict[str, List[float]]] = None

        if WHISPER_AVAILABLE:
            self._load_model()

        logger.info(f"Meeting Transcriber initialized with model: {model}")

    def _load_model(self):
        """Load Whisper model"""
        try:
            # Use faster-whisper for better performance
            self.model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type
            )
            logger.info(f"Loaded Whisper model: {self.model_name}")
        except Exception as e:
            logger.warning(f"Failed to load faster-whisper, falling back to standard whisper: {e}")
            try:
                self.model = whisper.load_model(self.model_name, device=self.device)
            except Exception as e:
                logger.error(f"Failed to load Whisper model: {e}")
                self.model = None

    def transcribe(
        self,
        audio_path: str,
        language: str = "en",
        identify_speakers: bool = False,
        word_timestamps: bool = True,
        known_voices: Optional[Dict[str, List[List[float]]]] = None,
        online: bool = False,
        local_name: Optional[str] = None,
    ) -> TranscriptionResult:
        """
        Transcribe audio file to text.

        Args:
            audio_path: Path to audio file
            language: Language code (en, es, fr, etc.)
            identify_speakers: Enable speaker diarization
            word_timestamps: Include word-level timestamps

        Returns:
            TranscriptionResult with segments and metadata
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        logger.info(f"Transcribing audio: {audio_path}")
        start_time = datetime.now()

        try:
            if not (self.model and WHISPER_AVAILABLE):
                raise TranscriptionUnavailableError(
                    "Whisper model is not available; cannot transcribe."
                )

            # Working out who speaks does not depend on the words, so it runs at the same time as
            # Whisper (both are heavy but release the interpreter lock): about the time of the slower one.
            self.last_speaker_voices = None
            self.last_local_label = None
            pool = future = None
            if identify_speakers:
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diarize")
                future = pool.submit(self._diarize_recording, audio_path, known_voices, online, local_name)
            try:
                # Speaker splitting needs word timings.
                t0 = time.monotonic()
                segments = self._transcribe_with_whisper(
                    audio_path,
                    language,
                    word_timestamps or identify_speakers,
                )
                logger.info(f"Speech to text took {time.monotonic() - t0:.1f}s")

                if future is not None:
                    t0 = time.monotonic()
                    diarized = future.result()
                    logger.info(f"Waited {time.monotonic() - t0:.1f}s more for the speaker analysis")
                    segments = self._identify_speakers(segments, audio_path, diarized)
            finally:
                if pool is not None:
                    pool.shutdown(wait=False)

            processing = (datetime.now() - start_time).total_seconds()
            duration = max((s.end_time for s in segments), default=0.0)

            result = TranscriptionResult(
                audio_file=audio_path,
                language=language,
                segments=segments,
                duration_seconds=duration,
                processing_seconds=processing,
                word_count=sum(len(s.text.split()) for s in segments),
                speaker_count=len(set(s.speaker for s in segments if s.speaker)),
                created_at=datetime.now(),
                speaker_voices=self.last_speaker_voices,
            )

            logger.info(
                f"Transcription complete: {result.word_count} words, "
                f"{duration:.1f}s of audio in {processing:.1f}s"
            )
            return result

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            raise

    def _transcribe_with_whisper(
        self,
        audio_path: str,
        language: str,
        word_timestamps: bool
    ) -> List[TranscriptSegment]:
        """Transcribe using Whisper model"""
        segments = []

        # Use faster-whisper
        if isinstance(self.model, WhisperModel):
            segments_iter, info = self.model.transcribe(
                audio_path,
                language=None if language == "auto" else language,
                word_timestamps=word_timestamps,
                vad_filter=True  # Voice activity detection
            )

            for segment in segments_iter:
                segments.append(TranscriptSegment(
                    id=len(segments),
                    start_time=segment.start,
                    end_time=segment.end,
                    text=segment.text.strip(),
                    speaker=None,
                    confidence=self._logprob_to_confidence(
                        segment.avg_logprob
                    ),
                    words=[
                        {"word": w.word, "start": w.start, "end": w.end}
                        for w in (segment.words or [])
                    ] or None,
                ))
        else:
            # Standard whisper
            result = self.model.transcribe(
                audio_path,
                language=language,
                word_timestamps=word_timestamps
            )

            for seg in result["segments"]:
                segments.append(TranscriptSegment(
                    id=seg["id"],
                    start_time=seg["start"],
                    end_time=seg["end"],
                    text=seg["text"].strip(),
                    speaker=None,
                    confidence=seg.get("confidence", 0.9)
                ))

        return segments

    @staticmethod
    def _logprob_to_confidence(avg_logprob: float) -> float:
        """Convert Whisper's average log-probability to a 0-1 score."""
        try:
            return round(max(0.0, min(1.0, math.exp(avg_logprob))), 4)
        except (TypeError, OverflowError):
            return 0.0

    def _load_diarization_pipeline(self):
        """Load the Pyannote speaker-diarization model on demand."""
        if self.diarization_pipeline is not None:
            return self.diarization_pipeline

        token = os.getenv("HF_TOKEN", "").strip()
        if not token:
            raise DiarizationUnavailableError(
                "Speaker diarization requires HF_TOKEN. Configure a Hugging "
                "Face read token and accept the model access conditions."
            )

        try:
            from pyannote.audio import Pipeline

            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=token,
            )
            if pipeline is None:
                raise RuntimeError("Pyannote returned no pipeline")

            device = self._move_to_gpu(pipeline)
            self._pipeline_device = device
            self.diarization_pipeline = pipeline
            logger.info(f"Loaded Pyannote speaker-diarization pipeline on {device}")
            return pipeline
        except Exception as exc:
            logger.error(f"Could not load Pyannote model: {exc}")
            raise DiarizationUnavailableError(
                "Could not load the Pyannote model. Check HF_TOKEN, model "
                "access permissions, and network connectivity."
            ) from exc

    @staticmethod
    def _pick_device() -> str:
        """
        Where the speaker model runs. DIARIZATION_DEVICE = auto (default: the GPU if there is one,
        which is Apple's "mps" on a Mac and CUDA elsewhere), or cpu / mps / cuda to force it.
        Docker containers on a Mac have no GPU, so they always end up on the CPU.
        """
        wanted = os.getenv("DIARIZATION_DEVICE", "auto").strip().lower()
        if wanted in ("cpu", "mps", "cuda"):
            return wanted
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def _move_to_gpu(self, pipeline) -> str:
        """Run the pipeline on the chosen device; any problem falls back to the CPU rather than failing."""
        device = self._pick_device()
        if device == "cpu":
            return "cpu"
        try:
            import torch
            pipeline.to(torch.device(device))
            return device
        except Exception as exc:
            logger.warning(f"Could not use the {device} GPU for speaker analysis, using the CPU: {exc}")
            return "cpu"

    def _diarize_file(self, audio_path: str, known_voices: Optional[Dict[str, List[List[float]]]] = None, online: bool = False):
        """Who speaks when, for a whole recording: ([(start, end, raw speaker)], {raw speaker: voice})."""
        t0 = time.monotonic()
        pipeline = self._load_diarization_pipeline()
        with readable_by_pyannote(audio_path) as readable:
            try:
                diarization, raw_voices = self._diarize(pipeline, readable)
            except Exception as exc:
                if self._pipeline_device == "cpu":
                    raise
                # a GPU operation that is not supported: do it on the CPU instead of failing the job
                logger.warning(f"Speaker analysis failed on {self._pipeline_device} ({exc}); retrying on the CPU")
                import torch
                pipeline.to(torch.device("cpu"))
                self._pipeline_device = "cpu"
                diarization, raw_voices = self._diarize(pipeline, readable)
            turns = [
                (turn.start, turn.end, raw)
                for turn, _, raw in diarization.itertracks(yield_label=True)
            ]
            if known_voices:
                try:
                    turns, raw_voices = self._name_turns(pipeline, readable, turns, raw_voices, known_voices, online)
                except Exception as exc:   # an extra: the clusters above are still good
                    logger.warning(f"Could not match individual turns to saved voices: {exc}")
        logger.info(f"Speaker analysis took {time.monotonic() - t0:.1f}s")
        return turns, raw_voices

    TURN_MIN_SECONDS = 1.2
    TURN_MAX_SECONDS = 8.0

    @staticmethod
    def _cos(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def _name_turns(self, pipeline, audio_path, turns, raw_voices, known_voices, online=False):
        """
        Recognise people turn by turn. The diarizer groups voices that sound alike into one speaker, so in a
        meeting with many people several of them can end up as one. Each turn is therefore also compared
        with the saved voices: a clear match gives the turn that person's name ("known:Name"), the rest
        stay with the diarizer's speaker.
        """
        import numpy as np
        import torch
        from pyannote.core import Segment

        min_score = float(os.getenv("TURN_MATCH_MIN", "0.5"))
        audio, embed = pipeline._audio, pipeline._embedding
        vectors = {}                                   # turn index -> its voice
        for i, (start, end, _raw) in enumerate(turns):
            if end - start < self.TURN_MIN_SECONDS:
                continue
            wave, _sr = audio.crop(audio_path, Segment(start, min(end, start + self.TURN_MAX_SECONDS)), mode="pad")
            with torch.no_grad():
                vec = np.asarray(embed(wave[None]))[0].astype(float)
            if np.all(np.isfinite(vec)) and np.linalg.norm(vec) > 0:
                vectors[i] = vec / np.linalg.norm(vec)
        names = [n for n, samples in known_voices.items() if samples]
        if not vectors or not names:
            return turns, raw_voices
        order = sorted(vectors)
        stack = np.array([vectors[i] for i in order])
        scores = np.zeros((len(names), len(order)))    # person x turn: best match to any saved sample
        for r, name in enumerate(names):
            for sample in known_voices[name]:
                sv = np.asarray(sample, dtype=float)
                if sv.shape == stack[0].shape and np.linalg.norm(sv) > 0:
                    scores[r] = np.maximum(scores[r], stack @ (sv / np.linalg.norm(sv)))
        # Some saved voices (those with several samples, or a "generic" sound) match everything a little.
        # With enough turns, judge each person against how they usually score in this recording.
        if len(order) >= 8:
            ranking = (scores - scores.mean(axis=1, keepdims=True)) / np.maximum(scores.std(axis=1, keepdims=True), 0.05)
            margin = float(os.getenv("TURN_MATCH_Z_MARGIN", "0.5"))
        else:
            ranking, margin = scores, float(os.getenv("TURN_MATCH_MARGIN", "0.06"))

        chosen = {}
        for col, i in enumerate(order):
            ranked = np.argsort(-ranking[:, col])
            best = ranked[0]
            second = ranking[ranked[1], col] if len(ranked) > 1 else -np.inf
            by_score = np.sort(scores[:, col])[::-1]
            runner_up = by_score[1] if len(by_score) > 1 else 0.0
            if scores[best, col] >= min_score and ranking[best, col] - second >= margin:
                chosen[i] = names[best]
            elif online and scores[best, col] >= 0.6 and scores[best, col] - runner_up >= 0.12:
                chosen[i] = names[best]          # a plainly clear match, even when the recording is short of turns
        out, found = [], {}
        for i, (start, end, raw) in enumerate(turns):
            if i in chosen:
                label = f"known:{chosen[i]}"
                found.setdefault(label, []).append(vectors[i])
                out.append((start, end, label))
            else:
                out.append((start, end, raw))
        voices = dict(raw_voices)
        if online:
            # A speaker made of turns that fit different people, or that do not fit the person most of its
            # turns match, is several people the diarizer blended. Its single blended voice must not be
            # given any one name, so it is dropped: its clear turns keep their own matches and the rest stay
            # unnamed ("Speaker N") rather than being forced onto someone.
            index = {n: r for r, n in enumerate(names)}
            members = {}
            for col, i in enumerate(order):
                members.setdefault(turns[i][2], []).append((col, i))
            for raw, rows in members.items():
                people = {chosen[i] for _, i in rows if i in chosen}
                if not people:
                    continue
                if len(people) > 1:
                    voices.pop(raw, None)
                    continue
                who = next(iter(people))
                misfits = sum(1 for col, i in rows if i not in chosen and scores[index[who], col] < 0.5)
                if misfits and misfits / len(rows) >= 0.25:
                    voices.pop(raw, None)
        for label, vecs in found.items():
            voices[label] = [float(x) for x in np.mean(np.array(vecs), axis=0)]
        logger.info(f"Matched {sum(len(v) for v in found.values())} turns to {len(found)} saved voices")
        return out, voices

    # ------------------------------------------------------------------ online meetings

    LOCAL_OVER_REMOTE = 1.5      # the microphone must be this much louder than the tab to be "you"
    LOCAL_MIN_GAP = 0.3          # seconds: shorter silences inside your own speech do not split it
    LOCAL_MIN_TURN = 0.25
    LOCAL_VOICE_MAX_SECONDS = 20.0
    LOCAL_VOICE_MIN_MATCH = 0.3  # below this the microphone voice contradicts the account's saved voice

    def _diarize_recording(self, audio_path, known_voices=None, online=False, local_name=None):
        """
        Who speaks when. Online-meeting recordings keep the microphone and the meeting tab on separate
        channels, which tells "me" from "everyone else" by channel instead of by a blended voice; any other
        recording (and one without separate channels) is analysed as a whole, as before.
        """
        if online:
            try:
                found = self._diarize_online(audio_path, known_voices, local_name)
                if found is not None:
                    return found
            except Exception as exc:       # never lose the transcript over this: analyse the mixed audio
                logger.warning(f"Could not separate the channels of the online recording: {exc}")
        return self._diarize_file(audio_path, known_voices, online=online)

    @staticmethod
    @contextmanager
    def _channel_files(audio_path: str):
        """Yield (left, right) mono 16 kHz WAV paths when the two channels really differ, else None."""
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, name in enumerate(("left", "right")):
                out = os.path.join(tmp, f"{name}.wav")
                proc = subprocess.run(
                    ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", audio_path, "-vn",
                     "-af", f"pan=mono|c0=c{index}", "-ar", "16000", out],
                    capture_output=True, text=True, timeout=1800,
                )
                if proc.returncode != 0 or not os.path.exists(out):
                    yield None
                    return
                paths.append(out)
            left, _ = sf.read(paths[0], dtype="float32")
            right, _ = sf.read(paths[1], dtype="float32")
            n = min(len(left), len(right))
            if n < 16000:
                yield None
                return
            left, right = left[:n], right[:n]
            total = float(np.sqrt(np.mean((left + right) ** 2))) + 1e-9
            difference = float(np.sqrt(np.mean((left - right) ** 2)))
            # Two identical channels (a recording from before the channels were separated, or without a
            # microphone) have no difference: nothing to separate.
            yield (paths[0], paths[1]) if difference / total > 0.25 else None

    @staticmethod
    def _frame_energy(path: str, hop: float = 0.02):
        import numpy as np
        import soundfile as sf
        data, rate = sf.read(path, dtype="float32")
        size = max(1, int(rate * hop))
        frames = len(data) // size
        return np.sqrt(np.mean(data[: frames * size].reshape(frames, size) ** 2, axis=1)), hop

    def _local_turns(self, left_path: str, right_path: str):
        """[(start, end, LOCAL)] where the microphone is clearly the louder side: you are the one speaking."""
        import numpy as np
        e_left, hop = self._frame_energy(left_path)
        e_right, _ = self._frame_energy(right_path)
        n = min(len(e_left), len(e_right))
        e_left, e_right = e_left[:n], e_right[:n]
        if n == 0:
            return []
        floor = max(float(np.percentile(e_left, 95)) * 0.1, 1e-4)
        active = (e_left > floor) & (e_left > self.LOCAL_OVER_REMOTE * e_right)
        turns, start, last = [], None, None
        for i, on in enumerate(active):
            if on:
                if start is None:
                    start = i
                last = i
            elif start is not None and (i - last) * hop > self.LOCAL_MIN_GAP:
                turns.append((start * hop, (last + 1) * hop))
                start = None
        if start is not None:
            turns.append((start * hop, (last + 1) * hop))
        return [(a, b, LOCAL) for a, b in turns if b - a >= self.LOCAL_MIN_TURN]

    def _local_voice(self, pipeline, left_path: str, turns):
        """One voice embedding from the first stretches of your own speech (None if there is too little)."""
        import numpy as np
        import torch
        from pyannote.core import Segment
        waves, total = [], 0.0
        for start, end, _ in turns:
            if end - start < 0.8:
                continue
            stop = min(end, start + (self.LOCAL_VOICE_MAX_SECONDS - total))
            wave, _sr = pipeline._audio.crop(left_path, Segment(start, stop), mode="pad")
            waves.append(wave)
            total += stop - start
            if total >= self.LOCAL_VOICE_MAX_SECONDS:
                break
        if total < 2.0:
            return None
        with torch.no_grad():
            vec = np.asarray(pipeline._embedding(torch.cat(waves, dim=1)[None]))[0].astype(float)
        return [float(x) for x in vec] if np.all(np.isfinite(vec)) else None

    def _diarize_online(self, audio_path, known_voices, local_name):
        """
        Left = your microphone, right = the meeting tab. You are found by the microphone being louder; the
        other participants are separated from the tab channel alone, where your own voice never appears.
        Returns None when the channels are not separate.
        """
        with self._channel_files(audio_path) as pair:
            if pair is None:
                return None
            left, right = pair
            local = self._local_turns(left, right)
            others = {n: s for n, s in (known_voices or {}).items() if not local_name or n.lower() != local_name.lower()}
            remote_turns, voices = self._diarize_file(right, others, online=True)
            voices = dict(voices)
            label = None
            if local:
                mine = self._local_voice(self._load_diarization_pipeline(), left, local)
                if mine is not None:
                    voices[LOCAL] = mine
                # the account's name only if the microphone voice does not contradict its saved voice
                saved = [s for n, s in (known_voices or {}).items() if local_name and n.lower() == local_name.lower()]
                contradicts = bool(mine and saved and max(self._cos(x, mine) for x in saved[0]) < self.LOCAL_VOICE_MIN_MATCH)
                label = None if contradicts or not local_name else local_name
            self.last_local_label = label
            # Anything the tab diarization found while you were speaking is the microphone leaking into it.
            logger.info(f"Online recording: {len(local)} stretches of your own speech, "
                        f"{len({t[2] for t in remote_turns})} other speakers")
            return remote_turns + local, voices

    def _identify_speakers(
        self,
        segments: List[TranscriptSegment],
        audio_path: str,
        diarized=None,
    ) -> List[TranscriptSegment]:
        """
        Assign speakers using audio-time overlap with Pyannote turns.

        When word timings are available, a segment that spans a speaker
        change is split at the change so each piece has one speaker.
        """
        if not segments:
            return segments

        turns, raw_voices = diarized if diarized is not None else self._diarize_file(audio_path)
        speaker_names: Dict[str, str] = {}

        def dominant(start: float, end: float) -> Optional[str]:
            overlaps: Dict[str, float] = {}
            for t_start, t_end, raw in turns:
                overlap = min(end, t_end) - max(start, t_start)
                if overlap > 0:
                    overlaps[raw] = overlaps.get(raw, 0.0) + overlap
            if not overlaps:
                return None
            raw = max(overlaps, key=overlaps.get)
            if raw not in speaker_names:
                if raw.startswith("known:"):
                    speaker_names[raw] = raw[len("known:"):]
                elif raw == LOCAL and getattr(self, "last_local_label", None):
                    speaker_names[raw] = self.last_local_label
                else:
                    taken = sum(1 for r, n in speaker_names.items() if re.fullmatch(r"Speaker \d+", n))
                    speaker_names[raw] = f"Speaker {taken + 1}"
            return speaker_names[raw]

        result: List[TranscriptSegment] = []

        for segment in sorted(segments, key=lambda s: s.start_time):
            pieces = self._split_by_speaker(segment, dominant)
            result.extend(pieces)

        for index, segment in enumerate(result):
            segment.id = index
            segment.words = None

        self.last_speaker_voices = {
            speaker_names[raw]: vec
            for raw, vec in raw_voices.items()
            if raw in speaker_names
        } or None
        return result

    @staticmethod
    def _diarize(pipeline, audio_path: str):
        """Run diarization; also return one voice embedding per raw speaker."""
        try:
            out = pipeline(audio_path, return_embeddings=True)
        except TypeError:           # pipeline without embedding support
            out = pipeline(audio_path)
        if not isinstance(out, tuple):
            return out, {}
        diarization, embeddings = out
        voices: Dict[str, List[float]] = {}
        try:
            for raw, vec in zip(diarization.labels(), embeddings):
                values = [float(x) for x in vec]
                if values and all(math.isfinite(x) for x in values):
                    voices[raw] = values
        except Exception as exc:  # voices are best effort; labels still work
            logger.warning(f"Could not read speaker embeddings: {exc}")
            voices = {}
        return diarization, voices

    def extract_single_voice(self, audio_path: str, min_seconds: float = 1.5, max_other_share: float = 0.25) -> Dict[str, Any]:
        """
        The voice embedding of the one person speaking in a short recording (for "record my voice").
        Raises NoSpeechError when there is too little speech or more than one person is heard.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        pipeline = self._load_diarization_pipeline()
        with readable_by_pyannote(audio_path) as readable:
            diarization, voices = self._diarize(pipeline, readable)
            seconds: Dict[str, float] = {}
            for turn, _, raw in diarization.itertracks(yield_label=True):
                seconds[raw] = seconds.get(raw, 0.0) + float(turn.end - turn.start)
        seconds = {k: v for k, v in seconds.items() if k in voices}
        total = sum(seconds.values())
        if not seconds or total < min_seconds:
            raise NoSpeechError("Not enough speech was heard. Speak for at least 4 seconds.")
        best = max(seconds, key=seconds.get)
        other = (total - seconds[best]) / total
        if seconds[best] < min_seconds:
            raise NoSpeechError("Not enough speech was heard. Speak for at least 4 seconds.")
        if other > max_other_share:
            raise NoSpeechError("More than one voice was heard. Record again in a quiet place, on your own.")
        return {"embedding": voices[best], "speech_seconds": round(seconds[best], 1)}

    def extract_voices(
        self, audio_path: str, segments: List[Dict[str, Any]]
    ) -> Dict[str, List[float]]:
        """
        Voice embeddings for an already-transcribed meeting. Each diarization
        cluster is mapped to the transcript speaker it overlaps most, so the
        saved labels (and any names given to them) are kept.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        pipeline = self._load_diarization_pipeline()
        with readable_by_pyannote(audio_path) as readable:
            diarization, raw_voices = self._diarize(pipeline, readable)
        turns = [
            (turn.start, turn.end, raw)
            for turn, _, raw in diarization.itertracks(yield_label=True)
        ]
        overlap: Dict[str, Dict[str, float]] = {}   # label -> raw -> seconds
        for seg in segments:
            label = seg.get("speaker")
            if not label:
                continue
            for t_start, t_end, raw in turns:
                span = min(float(seg["end_time"]), t_end) - max(float(seg["start_time"]), t_start)
                if span > 0:
                    per = overlap.setdefault(label, {})
                    per[raw] = per.get(raw, 0.0) + span
        voices: Dict[str, List[float]] = {}
        for label, per in overlap.items():
            best = max(per, key=per.get)
            if best in raw_voices:
                voices[label] = raw_voices[best]
        return voices

    @staticmethod
    def _split_by_speaker(segment, dominant) -> List[TranscriptSegment]:
        """Split one segment into single-speaker pieces using word times."""
        words = segment.words or []

        if not words:
            segment.speaker = dominant(segment.start_time, segment.end_time)
            return [segment]

        groups = []  # [speaker, [words]]
        for word in words:
            speaker = dominant(word["start"], word["end"])
            if groups and (speaker is None or groups[-1][0] in (None, speaker)):
                # Unlabelled words join the current group; an unlabelled
                # group adopts the first labelled speaker that follows.
                if groups[-1][0] is None:
                    groups[-1][0] = speaker
                groups[-1][1].append(word)
            else:
                groups.append([speaker, [word]])

        if len(groups) == 1:
            segment.speaker = groups[0][0] or dominant(
                segment.start_time, segment.end_time
            )
            return [segment]

        pieces = []
        for speaker, group_words in groups:
            text = "".join(w["word"] for w in group_words).strip()
            if not text:
                continue
            pieces.append(
                TranscriptSegment(
                    id=0,
                    start_time=group_words[0]["start"],
                    end_time=group_words[-1]["end"],
                    text=text,
                    speaker=speaker,
                    confidence=segment.confidence,
                )
            )
        return pieces or [segment]

    def export_txt(self, result: TranscriptionResult, output_path: str):
        """Export transcription to plain text"""
        with open(output_path, "w", encoding="utf-8") as f:
            for segment in result.segments:
                speaker_prefix = f"[{segment.speaker}] " if segment.speaker else ""
                f.write(f"{speaker_prefix}{segment.text}\n")

        logger.info(f"Exported transcript to: {output_path}")

    def export_srt(self, result: TranscriptionResult, output_path: str):
        """Export transcription to SRT subtitle format"""
        with open(output_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(result.segments, 1):
                start = self._format_timestamp(segment.start_time, srt=True)
                end = self._format_timestamp(segment.end_time, srt=True)

                f.write(f"{i}\n")
                f.write(f"{start} --> {end}\n")
                f.write(f"{segment.text}\n\n")

        logger.info(f"Exported SRT to: {output_path}")

    def export_vtt(self, result: TranscriptionResult, output_path: str):
        """Export transcription to WebVTT format"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")

            for segment in result.segments:
                start = self._format_timestamp(segment.start_time)
                end = self._format_timestamp(segment.end_time)

                f.write(f"{start} --> {end}\n")
                f.write(f"{segment.text}\n\n")

        logger.info(f"Exported VTT to: {output_path}")

    def _format_timestamp(self, seconds: float, srt: bool = False) -> str:
        """Format timestamp for subtitles"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)

        if srt:
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        else:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
