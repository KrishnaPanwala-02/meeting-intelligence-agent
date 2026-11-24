"""
Meeting Summarization Agent
AI-powered meeting summarization using a locally hosted Ollama model.
"""

import json
import math
import os
import re
from datetime import datetime
from typing import List, Optional

import httpx
from loguru import logger

from ..models.schemas import (
    TranscriptionResult, MeetingSummary, KeyPoint, Decision
)

# Transcripts longer than this are condensed chunk-by-chunk first, so no
# part of a long meeting is silently dropped.
MAX_DIRECT_CHARS = 9000
CHUNK_CHARS = 6000

STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["topic", "description"],
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "owner": {"type": "string"},
                    "context": {"type": "string"},
                },
                "required": ["decision", "owner", "context"],
            },
        },
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "key_quotes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["key_points", "decisions", "next_steps", "key_quotes"],
}


class SummarizationError(RuntimeError):
    """The summary could not be generated (e.g. Ollama unavailable)."""


class MeetingSummarizer:
    """
    Meeting Summarization Agent

    Features:
    - Executive summary generation
    - Key discussion points / topics
    - Decision tracking
    - Verbatim quote extraction (validated against the transcript)
    - Next steps identification
    - Chunked condensation for long transcripts
    """

    def __init__(
        self,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2",
        timeout: Optional[float] = None,
    ):
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model
        self.timeout = timeout or float(
            os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")
        )
        logger.info(f"Meeting Summarizer initialized with model: {model}")

    async def summarize(
        self,
        transcript: TranscriptionResult,
        meeting_title: str = "Meeting",
        participants: Optional[List[str]] = None,
    ) -> MeetingSummary:
        """Generate a structured summary from the actual transcript."""
        logger.info(f"Generating summary for: {meeting_title}")

        full_transcript = "\n".join(
            f"[{seg.speaker or 'Speaker'}] {seg.text.strip()}"
            for seg in transcript.segments
            if seg.text and seg.text.strip()
        )
        if not full_transcript:
            raise SummarizationError("Transcript contains no text")

        source = await self._condense_if_long(full_transcript)

        executive_summary = await self._generate_executive_summary(
            source, meeting_title
        )
        structured = await self._extract_structured(source)

        key_points = self._clean_key_points(structured.get("key_points"))
        decisions = self._clean_decisions(structured.get("decisions"))
        next_steps = self._clean_lines(structured.get("next_steps"))[:10]
        key_quotes = self._verified_quotes(
            structured.get("key_quotes"), full_transcript
        )

        summary = MeetingSummary(
            meeting_title=meeting_title,
            executive_summary=executive_summary,
            key_points=key_points,
            decisions=decisions,
            next_steps=next_steps,
            key_quotes=key_quotes,
            participants=participants or [],
            duration_minutes=math.ceil(transcript.duration_seconds / 60),
            word_count=transcript.word_count,
            created_at=datetime.now(),
        )

        logger.info(
            f"Summary generated: {len(key_points)} key points, "
            f"{len(decisions)} decisions"
        )
        return summary

    FOLLOW_LINE = re.compile(r"^\W*(follows up|new|still open)\s*[:\-]\s*(.+)$", re.I)
    FOLLOW_EMPTY = re.compile(r"^\W*(nothing|none|n/?a|no connection|not mentioned|no earlier|no link)", re.I)
    FOLLOW_LINE_CHARS = 300

    async def follow_up(
        self,
        memory: str,
        meeting_title: str,
        number: Optional[int],
        executive_summary: str,
        decisions: Optional[List[str]] = None,
        next_steps: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        How this meeting continues the earlier ones in its folder, as up to three labelled lines
        ("Follows up:", "New:", "Still open:"), or None if the model finds no link. Kept apart from the
        summary itself so remembered facts never leak into it.
        """
        this = f"Meeting {number}" if number else "This meeting"
        parts = [f"{executive_summary}"]
        if decisions:
            parts.append("Decisions: " + "; ".join(decisions))
        if next_steps:
            parts.append("Next steps: " + "; ".join(next_steps))
        prompt = (
            "EARLIER MEETINGS\n" + memory + f"\n\nNEWEST MEETING ({this}, {meeting_title})\n"
            + "\n".join(parts) + "\n\n"
            "Reply with these three short lines and nothing else:\n"
            "Follows up: <what the newest meeting says about something decided or assigned earlier, "
            "naming the earlier meeting from the EARLIER MEETINGS list, never the newest meeting itself; "
            "or 'nothing'>\n"
            "New: <what is new in the newest meeting and was not in the earlier ones>\n"
            "Still open: <earlier tasks the newest meeting did not mention; or 'nothing'>\n"
            "Each line must be copied from or directly supported by the text above. Do not combine "
            "different tasks or decisions. Never guess."
        )
        return self._clean_follow_up(await self._call_ollama(prompt, num_predict=260), number)

    @classmethod
    def _clean_follow_up(cls, text: str, number: Optional[int] = None) -> Optional[str]:
        """
        Keep the labelled lines that say something. Without a 'Follows up' or 'Still open' line there is no
        link. A line that points at the newest meeting itself as the earlier one is a slip and is dropped.
        """
        own = re.compile(rf"\bMeeting {number}\b", re.I) if number else None
        names = {"follows up": "Follows up", "new": "New", "still open": "Still open"}
        found = {}
        for raw in (text or "").splitlines():
            m = cls.FOLLOW_LINE.match(raw.strip())
            if not m:
                continue
            body = m.group(2).strip().lstrip("*_-• ").rstrip("*_ ").strip()      # no markdown bold or bullets
            label = names[m.group(1).lower()]
            if own is not None and label != "New" and own.search(body):
                continue
            if body and not cls.FOLLOW_EMPTY.match(body) and label not in found:
                found[label] = body[: cls.FOLLOW_LINE_CHARS]
        if "Follows up" not in found and "Still open" not in found:
            return None
        return "\n".join(f"{label}: {found[label]}" for label in ("Follows up", "New", "Still open") if label in found)

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------

    async def _condense_if_long(self, transcript: str) -> str:
        """Condense very long transcripts chunk-by-chunk, keeping all parts."""
        if len(transcript) <= MAX_DIRECT_CHARS:
            return transcript

        chunks = self._chunk_lines(transcript, CHUNK_CHARS)
        logger.info(f"Long transcript: condensing {len(chunks)} chunks")
        notes = []
        for index, chunk in enumerate(chunks, 1):
            prompt = (
                "Condense this part of a meeting transcript into detailed "
                "notes. Keep speaker names, decisions, commitments, "
                "deadlines, names, numbers and technical details. Do not "
                "add anything that is not in the text.\n\n"
                f"Transcript part {index} of {len(chunks)}:\n{chunk}"
            )
            notes.append(await self._call_ollama(prompt, num_predict=900))
        return "\n\n".join(notes)

    @staticmethod
    def _chunk_lines(text: str, size: int) -> List[str]:
        chunks, current, length = [], [], 0
        for line in text.split("\n"):
            if current and length + len(line) > size:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(line)
            length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _generate_executive_summary(
        self, transcript: str, title: str
    ) -> str:
        base = (
            "Write a concise executive summary (1-3 sentences) of what was "
            "said in this meeting. The transcript may be short: always "
            "summarize whatever is there, using ONLY facts stated in it. "
            "Do not guess the meeting's purpose or add anything that is "
            "not present. Output only the summary text.\n\n"
            f"Meeting title: {title}\n\nTranscript:\n{transcript}"
        )
        refusal = re.compile(
            r"^(no summary|i (cannot|can't|am unable)|unable to|there is "
            r"(not enough|insufficient))", re.I
        )
        summary = ""
        for _ in range(2):
            summary = (await self._call_ollama(base, num_predict=300)).strip()
            if summary and not refusal.match(summary):
                return summary
        raise SummarizationError(
            "The model did not produce a usable executive summary"
        )

    async def _extract_structured(self, transcript: str) -> dict:
        prompt = (
            "Analyse this meeting transcript and return JSON with:\n"
            "- key_points: the main topics discussed, each with a short "
            "topic and a one-sentence description.\n"
            "- decisions: choices or agreements explicitly made by the "
            "group (NOT individual to-do items). Include owner (use "
            "'TBD' if unknown) and context. Use an empty list if no "
            "decision was made.\n"
            "- next_steps: concrete follow-up actions, as short "
            "statements, each ideally naming who does it. Empty list if "
            "none.\n"
            "- key_quotes: up to 3 important sentences copied EXACTLY "
            "from the transcript, without the [Speaker] prefix. Empty "
            "list if none stand out.\n"
            "Keep it short: at most 6 key points, 5 decisions and 8 next "
            "steps. Use only information in the transcript. Never invent "
            "content.\n\n"
            f"Transcript:\n{transcript}"
        )
        data = None
        for attempt in range(2):
            raw = await self._call_ollama(
                prompt, schema=STRUCTURED_SCHEMA, num_predict=1500
            )
            data = self._parse_json(raw)
            if isinstance(data, dict):
                break
            logger.warning(
                f"Structured summary was not valid JSON (attempt {attempt + 1}); "
                f"{len(raw)} characters"
            )
            # Small models sometimes ramble until the length limit; ask for less.
            prompt += (
                "\n\nBe brief: at most 5 key points, 4 decisions, 6 next "
                "steps and 3 quotes. Keep every item to one short sentence."
            )
        if not isinstance(data, dict):
            raise SummarizationError("Ollama returned malformed summary data")
        return data

    @staticmethod
    def _parse_json(raw: str):
        """Parse the model's JSON; if it was cut off, keep everything that was complete."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        if start < 0:
            return None
        raw = raw[start:]
        try:
            return json.loads(raw, strict=False)
        except json.JSONDecodeError:
            pass
        # Walk back to successively earlier cut points and close what is open.
        cuts = [i + 1 for i, ch in enumerate(raw) if ch in ",}]\""]
        for cut in reversed(cuts[-400:]):
            piece = raw[:cut].rstrip().rstrip(",")
            stack, in_str, esc = [], False, False
            for ch in piece:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch in "{[":
                    stack.append("}" if ch == "{" else "]")
                elif ch in "}]" and stack:
                    stack.pop()
            if in_str:
                continue
            try:
                data = json.loads(piece + "".join(reversed(stack)))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    async def _call_ollama(
        self,
        prompt: str,
        schema: Optional[dict] = None,
        num_predict: int = 800,
    ) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0, "num_predict": num_predict, "repeat_penalty": 1.1},
        }
        if schema:
            payload["format"] = schema
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.ollama_host}/api/generate", json=payload
                )
                response.raise_for_status()
                return response.json().get("response", "") or ""
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(f"Ollama API error: {exc!r}")
            raise SummarizationError(
                "The Ollama service is unavailable or failed to respond"
            ) from exc

    # ------------------------------------------------------------------
    # Cleaning / validation of model output
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_lines(items) -> List[str]:
        seen, out = set(), []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, str):
                continue
            line = item.strip().lstrip("•-*").strip()
            if len(line) < 3 or line.lower() in seen:
                continue
            if re.match(r"^(here (are|is)|please note|note:)", line, re.I):
                continue
            seen.add(line.lower())
            out.append(line)
        return out

    @staticmethod
    def _clean_key_points(items) -> List[KeyPoint]:
        points, seen = [], set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic", "")).strip() or "Discussion"
            desc = str(item.get("description", "")).strip()
            if not desc or (topic.lower(), desc.lower()) in seen:
                continue
            seen.add((topic.lower(), desc.lower()))
            points.append(KeyPoint(topic=topic, description=desc))
        return points[:10]

    @staticmethod
    def _clean_decisions(items) -> List[Decision]:
        decisions, seen = [], set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("decision", "")).strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            decisions.append(
                Decision(
                    decision=text,
                    owner=str(item.get("owner", "")).strip() or "TBD",
                    context=str(item.get("context", "")).strip(),
                )
            )
        return decisions[:10]

    @staticmethod
    def _verified_quotes(items, transcript: str) -> List[str]:
        """Keep only quotes that really appear in the transcript."""
        def norm(s: str) -> str:
            return re.sub(r"\W+", " ", s).strip().lower()

        haystack = norm(re.sub(r"\[[^\]]*\]", " ", transcript))
        quotes = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, str):
                continue
            quote = re.sub(r"^\[[^\]]*\]\s*", "", item.strip())
            if len(quote) >= 15 and norm(quote) in haystack:
                if quote not in quotes:
                    quotes.append(quote)
        return quotes[:5]
