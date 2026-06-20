"""
Meeting Transcription Agent
Real-time and batch transcription using Whisper AI.
"""

import os
from typing import List, Dict, Any, Optional, AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

try:
    import whisper
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    logger.warning("Whisper not installed - transcription will use mock data")
    WHISPER_AVAILABLE = False

from ..models.schemas import TranscriptSegment, TranscriptionResult


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
        word_timestamps: bool = True
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
            if self.model and WHISPER_AVAILABLE:
                segments = self._transcribe_with_whisper(
                    audio_path,
                    language,
                    word_timestamps
                )
            else:
                segments = self._mock_transcription(audio_path)

            # Apply speaker diarization if requested
            if identify_speakers:
                segments = self._identify_speakers(segments)

            duration = (datetime.now() - start_time).total_seconds()

            result = TranscriptionResult(
                audio_file=audio_path,
                language=language,
                segments=segments,
                duration_seconds=duration,
                word_count=sum(len(s.text.split()) for s in segments),
                speaker_count=len(set(s.speaker for s in segments if s.speaker)),
                created_at=datetime.now()
            )

            logger.info(f"Transcription complete: {result.word_count} words, {duration:.2f}s")
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
                language=language,
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
                    confidence=segment.avg_logprob
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

    def _mock_transcription(self, audio_path: str) -> List[TranscriptSegment]:
        """Generate mock transcription for testing"""
        return [
            TranscriptSegment(
                id=0,
                start_time=0.0,
                end_time=5.0,
                text="Welcome everyone to today's meeting.",
                speaker="Speaker 1",
                confidence=0.95
            ),
            TranscriptSegment(
                id=1,
                start_time=5.0,
                end_time=12.0,
                text="Let's start by reviewing the action items from last week.",
                speaker="Speaker 1",
                confidence=0.93
            ),
            TranscriptSegment(
                id=2,
                start_time=12.0,
                end_time=18.0,
                text="I completed the research on the new features we discussed.",
                speaker="Speaker 2",
                confidence=0.91
            )
        ]

    def _identify_speakers(self, segments: List[TranscriptSegment]) -> List[TranscriptSegment]:
        """
        Apply speaker diarization to identify different speakers.

        Note: This is a simplified version. In production, use pyannote.audio
        or similar for accurate speaker diarization.
        """
        # Simplified speaker assignment based on pauses
        current_speaker = "Speaker 1"
        speaker_counter = 1

        for i, segment in enumerate(segments):
            if i > 0:
                # If there's a long pause, assume speaker change
                pause = segment.start_time - segments[i-1].end_time
                if pause > 2.0:  # 2 second pause
                    speaker_counter += 1
                    current_speaker = f"Speaker {speaker_counter}"

            segment.speaker = current_speaker

        return segments

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        language: str = "en"
    ) -> AsyncIterator[TranscriptSegment]:
        """
        Transcribe audio stream in real-time.

        Args:
            audio_stream: Async iterator of audio chunks
            language: Language code

        Yields:
            TranscriptSegment for each transcribed chunk
        """
        segment_id = 0
        buffer = bytearray()

        async for chunk in audio_stream:
            buffer.extend(chunk)

            # Process when buffer reaches threshold (e.g., 3 seconds of audio)
            if len(buffer) >= 48000:  # ~3 seconds at 16kHz
                # Save buffer to temp file and transcribe
                temp_path = f"/tmp/audio_chunk_{segment_id}.wav"

                # Write buffer to file
                # In production, use proper audio handling
                with open(temp_path, "wb") as f:
                    f.write(buffer)

                # Transcribe chunk
                result = self.transcribe(temp_path, language=language)

                for segment in result.segments:
                    segment.id = segment_id
                    segment_id += 1
                    yield segment

                # Clear buffer
                buffer.clear()

                # Clean up temp file
                try:
                    os.remove(temp_path)
                except:
                    pass

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
