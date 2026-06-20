"""
Meeting Summarization Agent
AI-powered meeting summarization using Ollama LLM.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
from loguru import logger
import httpx

from ..models.schemas import (
    TranscriptionResult, MeetingSummary, KeyPoint, Decision
)


class MeetingSummarizer:
    """
    Meeting Summarization Agent

    Features:
    - Executive summary generation
    - Key discussion points extraction
    - Decision tracking
    - Quote extraction
    - Next steps identification
    """

    def __init__(
        self,
        ollama_host: str = "http://localhost:11434",
        model: str = "llama3.2"
    ):
        """
        Initialize Meeting Summarizer

        Args:
            ollama_host: Ollama API host
            model: LLM model to use
        """
        self.ollama_host = ollama_host
        self.model = model
        logger.info(f"Meeting Summarizer initialized with model: {model}")

    async def summarize(
        self,
        transcript: TranscriptionResult,
        meeting_title: str = "Meeting",
        participants: Optional[List[str]] = None
    ) -> MeetingSummary:
        """
        Generate comprehensive meeting summary.

        Args:
            transcript: Transcription result
            meeting_title: Title of the meeting
            participants: List of participant names/emails

        Returns:
            MeetingSummary with all extracted information
        """
        logger.info(f"Generating summary for: {meeting_title}")

        # Combine transcript segments into full text
        full_transcript = "\n".join([
            f"[{seg.speaker or 'Speaker'}] {seg.text}"
            for seg in transcript.segments
        ])

        # Generate different components
        executive_summary = await self._generate_executive_summary(
            full_transcript, meeting_title
        )

        key_points = await self._extract_key_points(full_transcript)

        decisions = await self._extract_decisions(full_transcript)

        next_steps = await self._extract_next_steps(full_transcript)

        key_quotes = await self._extract_quotes(full_transcript)

        summary = MeetingSummary(
            meeting_title=meeting_title,
            executive_summary=executive_summary,
            key_points=key_points,
            decisions=decisions,
            next_steps=next_steps,
            key_quotes=key_quotes,
            participants=participants or [],
            duration_minutes=int(transcript.duration_seconds / 60),
            word_count=transcript.word_count,
            created_at=datetime.now()
        )

        logger.info(f"Summary generated: {len(key_points)} key points, {len(decisions)} decisions")
        return summary

    async def _generate_executive_summary(
        self,
        transcript: str,
        title: str
    ) -> str:
        """Generate 2-3 sentence executive summary"""
        prompt = f"""
        Generate a concise 2-3 sentence executive summary of this meeting:

        Meeting Title: {title}

        Transcript:
        {transcript[:3000]}  # Limit for context

        Focus on:
        1. Main purpose of the meeting
        2. Key outcomes or decisions
        3. Overall status or progress

        Provide only the summary, no additional commentary.
        """

        try:
            response = await self._call_ollama(prompt)
            return response.strip()
        except Exception as e:
            logger.error(f"Error generating executive summary: {e}")
            return "Meeting summary could not be generated."

    async def _extract_key_points(self, transcript: str) -> List[KeyPoint]:
        """Extract main discussion points"""
        prompt = f"""
        Extract the main discussion points from this meeting transcript.
        Format each point as a bullet point with topic and description.

        Transcript:
        {transcript[:4000]}

        Return as JSON array:
        [
            {{"topic": "Topic Name", "description": "Brief description of discussion"}},
            ...
        ]
        """

        try:
            response = await self._call_ollama(prompt)
            points = self._parse_key_points(response)
            return points
        except Exception as e:
            logger.error(f"Error extracting key points: {e}")
            return []

    async def _extract_decisions(self, transcript: str) -> List[Decision]:
        """Extract decisions made during the meeting"""
        prompt = f"""
        Extract all decisions made during this meeting.

        Transcript:
        {transcript[:4000]}

        For each decision, identify:
        - What was decided
        - Who is responsible (if mentioned)
        - Any context or reasoning

        Return as JSON array:
        [
            {{
                "decision": "What was decided",
                "owner": "Person responsible",
                "context": "Why or how this decision was made"
            }},
            ...
        ]
        """

        try:
            response = await self._call_ollama(prompt)
            decisions = self._parse_decisions(response)
            return decisions
        except Exception as e:
            logger.error(f"Error extracting decisions: {e}")
            return []

    async def _extract_next_steps(self, transcript: str) -> List[str]:
        """Extract next steps and follow-up actions"""
        prompt = f"""
        Extract the next steps and follow-up actions from this meeting.

        Transcript:
        {transcript[:4000]}

        Look for phrases like:
        - "Next, we should..."
        - "Moving forward..."
        - "The next step is..."
        - "We need to follow up on..."

        Return as a simple list of action-oriented statements.
        """

        try:
            response = await self._call_ollama(prompt)
            next_steps = [
                line.strip().lstrip('•-*').strip()
                for line in response.split('\n')
                if line.strip() and not line.strip().startswith(('#', 'Next'))
            ]
            return next_steps[:10]  # Limit to top 10
        except Exception as e:
            logger.error(f"Error extracting next steps: {e}")
            return []

    async def _extract_quotes(self, transcript: str) -> List[str]:
        """Extract important verbatim quotes"""
        # Extract segments that seem important
        # For now, return first few significant statements
        lines = transcript.split('\n')
        quotes = []

        for line in lines[:20]:  # Check first 20 lines
            if len(line) > 50 and any(word in line.lower() for word in
                                     ['important', 'critical', 'key', 'decision', 'agreed']):
                # Clean up speaker prefix
                text = line.split('] ', 1)[-1] if ']' in line else line
                quotes.append(text.strip())

            if len(quotes) >= 5:
                break

        return quotes

    async def _call_ollama(self, prompt: str) -> str:
        """Call Ollama API"""
        try:
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
        except Exception as e:
            logger.error(f"Ollama API error: {e}")
            raise

    def _parse_key_points(self, response: str) -> List[KeyPoint]:
        """Parse LLM response into KeyPoint objects"""
        import json
        import re

        points = []

        # Try to extract JSON
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                for item in data:
                    points.append(KeyPoint(
                        topic=item.get('topic', 'Discussion'),
                        description=item.get('description', '')
                    ))
                return points
            except json.JSONDecodeError:
                pass

        # Fallback: parse as bullet points
        for line in response.split('\n'):
            line = line.strip().lstrip('•-*').strip()
            if line and len(line) > 10:
                # Try to split topic and description
                if ':' in line:
                    topic, desc = line.split(':', 1)
                    points.append(KeyPoint(
                        topic=topic.strip(),
                        description=desc.strip()
                    ))
                else:
                    points.append(KeyPoint(
                        topic="Discussion",
                        description=line
                    ))

        return points[:10]  # Limit to top 10

    def _parse_decisions(self, response: str) -> List[Decision]:
        """Parse LLM response into Decision objects"""
        import json
        import re

        decisions = []

        # Try to extract JSON
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                for item in data:
                    decisions.append(Decision(
                        decision=item.get('decision', ''),
                        owner=item.get('owner', 'TBD'),
                        context=item.get('context', '')
                    ))
                return decisions
            except json.JSONDecodeError:
                pass

        # Fallback: parse as bullet points
        for line in response.split('\n'):
            line = line.strip().lstrip('•-*').strip()
            if line and len(line) > 15:
                decisions.append(Decision(
                    decision=line,
                    owner="TBD",
                    context=""
                ))

        return decisions[:10]  # Limit to top 10
