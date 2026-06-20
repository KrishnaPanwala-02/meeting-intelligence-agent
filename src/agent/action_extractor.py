"""
Action Item Extraction Agent
Automatically detect and extract action items from meeting transcripts.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from loguru import logger
import httpx
import re

from ..models.schemas import ActionItem, TranscriptionResult


class ActionItemExtractor:
    """
    Action Item Extraction Agent

    Features:
    - Automatic action item detection
    - Assignee identification
    - Due date extraction
    - Priority inference
    - Dependency tracking
    """

    def __init__(
        self,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2"
    ):
        """Initialize Action Item Extractor"""
        self.ollama_host = ollama_host
        self.model = model
        self.action_patterns = [
            r"we need to",
            r"i'll follow up",
            r"can you",
            r"please",
            r"let's make sure",
            r"action item",
            r"todo",
            r"task",
            r"@\w+",  # @ mentions
        ]
        logger.info("Action Item Extractor initialized")

    async def extract(
        self,
        transcript: TranscriptionResult,
        use_ai: bool = True
    ) -> List[ActionItem]:
        """
        Extract action items from transcript.

        Args:
            transcript: Meeting transcription
            use_ai: Use AI for extraction (vs pattern matching)

        Returns:
            List of ActionItem objects
        """
        logger.info("Extracting action items from transcript")

        if use_ai:
            action_items = await self._extract_with_ai(transcript)
        else:
            action_items = self._extract_with_patterns(transcript)

        logger.info(f"Extracted {len(action_items)} action items")
        return action_items

    async def _extract_with_ai(
        self,
        transcript: TranscriptionResult
    ) -> List[ActionItem]:
        """Use AI to extract action items"""
        full_text = "\n".join([
            f"[{seg.speaker or 'Speaker'}] {seg.text}"
            for seg in transcript.segments
        ])

        prompt = f"""
        Extract all action items from this meeting transcript.

        Transcript:
        {full_text[:4000]}

        For each action item, identify:
        - Task description
        - Assignee (person responsible, if mentioned)
        - Due date (if mentioned)
        - Priority (high/medium/low based on context)

        Return as JSON array:
        [
            {{
                "task": "Description of the task",
                "assignee": "Person's name or 'TBD'",
                "due_date": "Date string or null",
                "priority": "high/medium/low"
            }},
            ...
        ]
        """

        try:
            response = await self._call_ollama(prompt)
            items = self._parse_action_items(response)
            return items
        except Exception as e:
            logger.error(f"AI extraction error: {e}")
            return []

    def _extract_with_patterns(
        self,
        transcript: TranscriptionResult
    ) -> List[ActionItem]:
        """Extract action items using pattern matching"""
        items = []

        for segment in transcript.segments:
            text_lower = segment.text.lower()

            # Check if segment contains action indicators
            if any(re.search(pattern, text_lower) for pattern in self.action_patterns):
                # Extract assignee
                assignee = segment.speaker or "TBD"

                # Check for @ mentions
                mentions = re.findall(r'@(\w+)', segment.text)
                if mentions:
                    assignee = mentions[0]

                # Infer priority
                priority = "medium"
                if any(word in text_lower for word in ['urgent', 'asap', 'critical']):
                    priority = "high"
                elif any(word in text_lower for word in ['later', 'eventually', 'nice']):
                    priority = "low"

                items.append(ActionItem(
                    task=segment.text,
                    assignee=assignee,
                    due_date=None,
                    priority=priority,
                    created_at=datetime.now()
                ))

        return items

    async def _call_ollama(self, prompt: str) -> str:
        """Call Ollama API"""
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.ollama_host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False
                }
            )
            response.raise_for_status()
            return response.json()["response"]

    def _parse_action_items(self, response: str) -> List[ActionItem]:
        """Parse AI response into ActionItem objects"""
        import json

        items = []

        # Try JSON parsing
        try:
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                for item in data:
                    due_date = None
                    if item.get('due_date'):
                        try:
                            due_date = datetime.fromisoformat(item['due_date'])
                        except:
                            # Try relative dates
                            if 'week' in item['due_date'].lower():
                                due_date = datetime.now() + timedelta(weeks=1)
                            elif 'day' in item['due_date'].lower():
                                due_date = datetime.now() + timedelta(days=1)

                    items.append(ActionItem(
                        task=item.get('task', ''),
                        assignee=item.get('assignee', 'TBD'),
                        due_date=due_date,
                        priority=item.get('priority', 'medium'),
                        created_at=datetime.now()
                    ))
        except Exception as e:
            logger.warning(f"Failed to parse action items: {e}")

        return items

    async def create_task(
        self,
        item: ActionItem,
        platform: str = "linear"
    ) -> Dict[str, Any]:
        """
        Create task in project management platform.

        Args:
            item: ActionItem to create
            platform: Platform (linear, jira, asana, notion)

        Returns:
            Created task info
        """
        # In production, integrate with actual APIs
        logger.info(f"Creating task in {platform}: {item.task}")

        return {
            "platform": platform,
            "task_id": f"{platform}-{hash(item.task)}",
            "url": f"https://{platform}.com/task/{hash(item.task)}",
            "created": True
        }
