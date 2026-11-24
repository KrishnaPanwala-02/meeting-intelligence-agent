
"""
Action Item Extraction Agent
Automatically detect and extract action items from meeting transcripts.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, date, timedelta
from loguru import logger
import httpx
import re
import json
import os

from ..models.schemas import ActionItem, TranscriptionResult


MAX_CHUNK_CHARS = 10000


class ActionExtractionError(RuntimeError):
    """Action items could not be extracted (e.g. Ollama unavailable)."""


class ActionItemExtractor:
    """
    Action Item Extraction Agent.

    Features:
    - Automatic action item detection
    - Assignee identification
    - Deadline phrase extraction
    - Deterministic deadline resolution
    - Priority inference
    - Dependency tracking
    """

    def __init__(
        self,
        ollama_host: str = "http://host.docker.internal:11434",
        model: str = "llama3.2",
    ):
        self.ollama_host = ollama_host.rstrip("/")
        self.model = model

        self.action_patterns = [
            r"\bi will\b",
            r"\bi'll\b",
            r"\bwe need to\b",
            r"\bwe will\b",
            r"\bwe'll\b",
            r"\bi need to\b",
            r"\bi must\b",
            r"\bi should\b",
            r"\bi have to\b",
            r"\bi am going to\b",
            r"\bi'm going to\b",
            r"\bi plan to\b",
            r"\bi commit to\b",
            r"\bi'll follow up\b",
            r"\bcan you\b",
            r"\bplease\b",
            r"\blet's make sure\b",
            r"\baction item\b",
            r"\bto-do\b",
            r"\btodo\b",
            r"\btask\b",
            r"@\w+",
        ]

        logger.info("Action Item Extractor initialized")

    async def extract(
        self,
        transcript: TranscriptionResult,
        use_ai: bool = True,
        reference_date: Optional[date] = None,
    ) -> List[ActionItem]:
        """
        Extract action items without saving them to the database.

        reference_date is the actual meeting date used to resolve relative
        deadlines such as "by Friday". It defaults to the transcript date.
        """

        logger.info("Extracting action items from transcript")

        if use_ai:
            action_items = await self._extract_with_ai(
                transcript, reference_date=reference_date
            )
        else:
            action_items = self._extract_with_patterns(transcript)

        logger.info("Extracted {} action items", len(action_items))
        return action_items

    def _get_transcript_text(
        self,
        transcript: TranscriptionResult,
    ) -> str:
        """
        Build transcript text from diarized segments when available.
        Fall back to the complete transcript when segments are empty.
        """

        segments = getattr(transcript, "segments", None) or []

        segment_lines = []

        for segment in segments:
            text = getattr(segment, "text", "")
            if not isinstance(text, str) or not text.strip():
                continue

            speaker = getattr(segment, "speaker", None) or "Speaker"
            segment_lines.append(f"[{speaker}] {text.strip()}")

        if segment_lines:
            return "\n".join(segment_lines)

        full_transcript = getattr(transcript, "transcript", "")
        if isinstance(full_transcript, str):
            return full_transcript.strip()

        return ""

    async def _extract_with_ai(
        self,
        transcript: TranscriptionResult,
        reference_date: Optional[date] = None,
    ) -> List[ActionItem]:
        """Extract from the whole transcript, chunking long ones."""

        full_text = self._get_transcript_text(transcript)

        if not full_text:
            logger.warning("Cannot extract actions: transcript text is empty")
            return []

        if reference_date is None:
            created_at = getattr(transcript, "created_at", None)
            reference_date = (
                created_at.date()
                if isinstance(created_at, datetime)
                else None
            )

        chunks = self._chunk_text(full_text, MAX_CHUNK_CHARS)
        items: List[ActionItem] = []
        seen = set()

        for chunk in chunks:
            for item in await self._extract_chunk(chunk, reference_date):
                key = re.sub(r"\W+", " ", item.task.lower()).strip()
                if key and key not in seen:
                    seen.add(key)
                    items.append(item)

        return items

    @staticmethod
    def _chunk_text(text: str, size: int) -> List[str]:
        """Split on line boundaries so no part of the transcript is lost."""
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

    async def _extract_chunk(
        self,
        full_text: str,
        reference_date: Optional[date],
    ) -> List[ActionItem]:
        """Extract all distinct action items and preserve spoken deadlines."""

        if reference_date is None:
            logger.warning(
                "Transcript has no reference date; relative deadlines "
                "will remain unresolved"
            )

        prompt = f"""
You are a careful meeting action-item extraction system.

Extract EVERY distinct action item explicitly stated or clearly agreed
upon in the transcript below.

TRANSCRIPT:
{full_text}

RULES FOR COMPLETENESS:
1. Read the entire transcript before producing the result.
2. Return one JSON array containing every distinct action item.
3. Do not stop after the first action item, but return at most 15 items,
   each described in one short sentence.
4. Do not merge two different tasks into one item.
5. Include explicit commitments such as "I will finish X" and
   "I will prepare Y", even if the words "action item" are not used.
6. Do not invent tasks, assignees, or deadlines.
7. Exclude greetings, general discussion, suggestions that were not
   accepted, and completed background work that requires no further action.
8. Avoid duplicate entries for the same task.

FIELDS FOR EACH ITEM:
- task: concise description of the action
- assignee: the identified speaker/person, or "TBD"
- due_date_phrase: the original deadline wording from the transcript,
  or null if no deadline is stated
- priority: "low", "medium", or "high"

DEADLINE RULES:
1. Copy the deadline phrase exactly as spoken, allowing only removal of
   surrounding whitespace.
2. Examples: "by Friday", "next Monday", "tomorrow",
   "November 5, 2026".
3. Never calculate or invent a calendar date.
4. Do not use the meeting date as a task deadline.
5. If the deadline is ambiguous or absent, return null.
6. Words such as "soon" and "later" are not specific calendar deadlines.

OUTPUT:
Return ONLY a valid JSON array. Do not include Markdown fences,
explanations, or text outside the array.

Example structure:
[
  {{
    "task": "Complete the model training",
    "assignee": "Speaker 1",
    "due_date_phrase": "by Friday",
    "priority": "high"
  }},
  {{
    "task": "Prepare the project report",
    "assignee": "Speaker 2",
    "due_date_phrase": "by Monday",
    "priority": "medium"
  }}
]

The example illustrates the structure only. Extract the actual items
from the supplied transcript, not from the example.

If there are no action items, return [].
"""

        try:
            response = await self._call_ollama(prompt)

            logger.info(
                "Received Ollama action extraction response ({} characters)",
                len(response),
            )

            items = self._parse_action_items(
                response,
                reference_date=reference_date,
            )

            logger.info(
                "Parsed {} action items from Ollama response",
                len(items),
            )

            if not items:
                logger.warning(
                    "Ollama returned no parseable action items. "
                    "Check the model response and transcript."
                )

            return items

        except (httpx.HTTPError, ValueError) as exc:
            logger.error("AI extraction failed: {!r}", exc)
            raise ActionExtractionError(
                "The Ollama service is unavailable or returned an "
                "invalid response"
            ) from exc

    def _extract_with_patterns(
        self,
        transcript: TranscriptionResult,
    ) -> List[ActionItem]:
        """Extract likely action items using simple pattern matching."""

        items = []
        seen_tasks = set()

        segments = getattr(transcript, "segments", None) or []

        if segments:
            entries = [
                (
                    getattr(segment, "text", ""),
                    getattr(segment, "speaker", None) or "TBD",
                )
                for segment in segments
            ]
        else:
            text = getattr(transcript, "transcript", "") or ""
            entries = [(text, "TBD")]

        for segment_text, speaker in entries:
            if not isinstance(segment_text, str) or not segment_text.strip():
                continue

            sentences = re.split(r"(?<=[.!?])\s+", segment_text.strip())

            for sentence in sentences:
                text_lower = sentence.lower()

                if not any(
                    re.search(pattern, text_lower)
                    for pattern in self.action_patterns
                ):
                    continue

                task_key = re.sub(r"\W+", " ", text_lower).strip()

                if not task_key or task_key in seen_tasks:
                    continue

                seen_tasks.add(task_key)

                mentions = re.findall(r"@(\w+)", sentence)
                assignee = mentions[0] if mentions else speaker

                priority = "medium"

                if any(
                    word in text_lower
                    for word in ["urgent", "asap", "critical", "immediately"]
                ):
                    priority = "high"
                elif any(
                    word in text_lower
                    for word in ["low priority", "when possible", "optional"]
                ):
                    priority = "low"

                # Pattern matching does not invent a deadline.
                items.append(
                    ActionItem(
                        task=sentence.strip(),
                        assignee=assignee,
                        due_date=None,
                        priority=priority,
                        created_at=datetime.now(),
                    )
                )

        return items


    async def _call_ollama(self, prompt: str) -> str:
        """Call Ollama and require a JSON array of action items."""

        output_schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "assignee": {"type": "string"},
                    "due_date_phrase": {
                        "type": ["string", "null"]
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"]
                    }
                },
                "required": [
                    "task",
                    "assignee",
                    "due_date_phrase",
                    "priority"
                ]
            }
        }

        async with httpx.AsyncClient(timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "240"))) as client:
            response = await client.post(
                f"{self.ollama_host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": output_schema,
                    "options": {
                        "temperature": 0,
                        "repeat_penalty": 1.1,
                        "num_predict": 1000
                    }
                }
            )

            response.raise_for_status()
            payload = response.json()
            result = payload.get("response")

            if not isinstance(result, str) or not result.strip():
                raise ValueError("Ollama returned an empty response")

            return result


    def _resolve_due_date(
        self,
        phrase: Optional[str],
        reference_date: Optional[date],
    ) -> Optional[datetime]:
        """
        Resolve supported deadline phrases using a known reference date.

        Unsupported or ambiguous phrases return None.
        """

        if not isinstance(phrase, str) or reference_date is None:
            return None

        text = phrase.strip().lower()

        if not text:
            return None

        # Remove common prefixes and trailing punctuation.
        text = re.sub(r"\s+", " ", text)
        text = re.sub(
            r"^(?:(?:by|on|due(?: date is)?|deadline is|before|until|till|"
            r"no later than|latest by)\s+)+",
            "",
            text,
        ).strip()

        text = re.sub(r"[.!?,;]+$", "", text).strip()

        # "this friday" / "coming monday" / "the 15th" style wording.
        text = re.sub(
            r"^(?:the\s+)?(?:coming|this)\s+(?=(?:mon|tues|wednes|thurs|"
            r"fri|satur|sun)day$)",
            "",
            text,
        )
        text = re.sub(r"^the\s+(?=\d)", "", text)

        # ISO date or datetime.
        try:
            parsed = datetime.fromisoformat(
                text.replace("z", "+00:00")
            )
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass

        # Explicit dates with a year.
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(text, fmt).date()
                return datetime.combine(parsed_date, datetime.min.time())
            except ValueError:
                continue

        # Relative weekdays.
        weekdays = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }

        weekday_match = re.fullmatch(
            r"(next\s+)?"
            r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
            text,
        )

        if weekday_match:
            is_next = bool(weekday_match.group(1))
            target_weekday = weekdays[weekday_match.group(2)]

            days_ahead = (
                target_weekday - reference_date.weekday()
            ) % 7

            # A weekday without "next" means its next occurrence.
            # If it is today, use the following week's occurrence.
            if days_ahead == 0:
                days_ahead = 7

            # Explicit "next Friday" means the following week's Friday.
            if is_next:
                days_ahead += 7

            target_date = reference_date + timedelta(days=days_ahead)

            return datetime.combine(target_date, datetime.min.time())

        if text == "today":
            return datetime.combine(reference_date, datetime.min.time())

        if text == "tomorrow":
            return datetime.combine(
                reference_date + timedelta(days=1),
                datetime.min.time(),
            )

        extra = self._resolve_extended_phrase(text, reference_date)
        if extra is not None:
            return extra

        # Do not guess unsupported or ambiguous dates such as "soon",
        # "later", "next month" or "ASAP".
        logger.warning(
            "Could not safely resolve deadline phrase: {!r}",
            phrase,
        )
        return None

    _MONTHS = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3,
        "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
        "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
        "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11,
        "nov": 11, "december": 12, "dec": 12,
    }
    _NUMBER_WORDS = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
    }

    def _resolve_extended_phrase(
        self,
        text: str,
        reference: date,
    ) -> Optional[datetime]:
        """
        Resolve additional phrases deterministically.

        Conventions (documented, never guessed by the LLM):
        - "within/in N days|weeks" -> reference date + N days/weeks
        - "next week" / "end of next week" -> Friday of next week
        - "this week" / "end of (the) week" -> this Friday (None if the
          reference date is already after Friday)
        - "end of (the) month" -> last day of the reference month
        - "end of day", "eod", "tonight" -> the reference date
        - "day after tomorrow" -> reference + 2 days
        - "15 November" / "November 15th" without a year -> the next
          occurrence on or after the reference date
        - "next month", "soon", "later", "asap" -> None (ambiguous)
        """

        def at(d: date) -> datetime:
            return datetime.combine(d, datetime.min.time())

        if text in {"end of day", "end of the day", "eod", "tonight",
                    "close of business", "cob"}:
            return at(reference)

        if text == "day after tomorrow":
            return at(reference + timedelta(days=2))

        friday_this_week = reference + timedelta(
            days=4 - reference.weekday()
        )

        if text in {"next week", "end of next week", "the end of next week"}:
            return at(friday_this_week + timedelta(days=7))

        if text in {"this week", "end of week", "end of the week",
                    "the end of the week", "end of this week"}:
            return at(friday_this_week) if reference.weekday() <= 4 else None

        if text in {"end of month", "end of the month", "end of this month"}:
            first_next = (
                date(reference.year + 1, 1, 1)
                if reference.month == 12
                else date(reference.year, reference.month + 1, 1)
            )
            return at(first_next - timedelta(days=1))

        relative = re.fullmatch(
            r"(?:within|in)\s+(?:the next\s+)?(\d+|[a-z]+)\s+"
            r"(day|days|week|weeks)(?:\s+from now)?|"
            r"(\d+|[a-z]+)\s+(day|days|week|weeks)\s+from now",
            text,
        )
        if relative:
            raw = relative.group(1) or relative.group(3)
            unit = relative.group(2) or relative.group(4)
            amount = (
                int(raw) if raw.isdigit() else self._NUMBER_WORDS.get(raw)
            )
            if amount is not None and 0 < amount <= 366:
                days = amount * (7 if unit.startswith("week") else 1)
                return at(reference + timedelta(days=days))
            return None

        # Calendar dates: "15 november [2026]", "november 15th [2026]"
        cleaned = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text)
        cleaned = cleaned.replace(" of ", " ").replace(",", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        day = month = year = None
        m = re.fullmatch(r"(\d{1,2}) ([a-z]+)(?: (\d{4}))?", cleaned)
        if m:
            day, month_name, year = m.group(1), m.group(2), m.group(3)
        else:
            m = re.fullmatch(r"([a-z]+) (\d{1,2})(?: (\d{4}))?", cleaned)
            if m:
                month_name, day, year = m.group(1), m.group(2), m.group(3)

        if m and month_name in self._MONTHS:
            month = self._MONTHS[month_name]
            try:
                if year:
                    return at(date(int(year), month, int(day)))
                candidate = date(reference.year, month, int(day))
                if candidate < reference:
                    candidate = date(reference.year + 1, month, int(day))
                return at(candidate)
            except ValueError:
                return None  # e.g. 31 February

        return None

    def _parse_action_items(
        self,
        response: str,
        reference_date: Optional[date] = None,
    ) -> List[ActionItem]:
        """Parse a JSON array and resolve its original deadline phrases."""

        if not isinstance(response, str) or not response.strip():
            logger.warning("AI returned an empty action-item response")
            return []

        decoder = json.JSONDecoder()
        data = None

        # Locate the first complete JSON array, ignoring surrounding text.
        for match in re.finditer(r"\[", response):
            try:
                candidate, _ = decoder.raw_decode(response[match.start():])

                if isinstance(candidate, list):
                    data = candidate
                    break

            except json.JSONDecodeError:
                continue

        if data is None:
            # The answer may have been cut off: keep every complete item before the cut.
            salvaged, pos = [], 0
            while True:
                start = response.find("{", pos)
                if start < 0:
                    break
                try:
                    obj, used = decoder.raw_decode(response[start:])
                except json.JSONDecodeError:
                    pos = start + 1
                    continue
                if isinstance(obj, dict) and "task" in obj:
                    salvaged.append(obj)
                pos = start + used
            data = salvaged or None
        if data is None:
            logger.warning(
                "No valid JSON array found in Ollama response"
            )
            return []

        items = []
        seen_tasks = set()

        for item in data:
            if not isinstance(item, dict):
                continue

            task = item.get("task")

            if not isinstance(task, str) or not task.strip():
                continue

            task = task.strip()
            task_key = re.sub(r"\W+", " ", task.lower()).strip()

            if not task_key or task_key in seen_tasks:
                continue

            seen_tasks.add(task_key)

            # Only resolve the original phrase. Never trust an AI-calculated
            # due_date, since the model may calculate relative dates wrongly.
            due_date_phrase = item.get("due_date_phrase")
            due_date = self._resolve_due_date(
                due_date_phrase,
                reference_date,
            )

            priority = str(item.get("priority", "medium")).lower()

            if priority not in {"low", "medium", "high"}:
                priority = "medium"

            assignee = item.get("assignee", "TBD")

            if not isinstance(assignee, str) or not assignee.strip():
                assignee = "TBD"

            items.append(
                ActionItem(
                    task=task,
                    assignee=assignee.strip(),
                    due_date=due_date,
                    due_date_phrase=(
                        due_date_phrase.strip()
                        if isinstance(due_date_phrase, str)
                        and due_date_phrase.strip()
                        else None
                    ),
                    priority=priority,
                    created_at=datetime.now(),
                )
            )

        return items
