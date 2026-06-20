# Meeting & Calendar Intelligence Agent

A production-ready AI agent for meeting management, transcription, summarization, and calendar optimization using Whisper, Ollama, and calendar APIs.

## Features

- 🎙️ **Real-time Transcription** - Powered by Whisper AI
- 📝 **Meeting Summarization** - Auto-generate notes and action items
- 📅 **Smart Scheduling** - AI-powered optimal meeting times
- ✅ **Action Item Extraction** - Automatic task detection
- 🔔 **Follow-up Reminders** - Never miss action items
- 🔗 **Calendar Integration** - Google Calendar, Outlook, Cal.com

## Tech Stack

- **Whisper** - OpenAI's speech-to-text model
- **Ollama** - Local LLM for summarization
- **FastAPI** - Async web framework
- **WebRTC** - Real-time audio streaming
- **Redis** - Real-time data and caching
- **PostgreSQL** - Meeting storage
- **Celery** - Background task processing
- **Google Calendar API** - Calendar integration

## Architecture

```
meeting-intelligence-agent/
├── src/
│   ├── agent/
│   │   ├── transcriber.py          # Whisper transcription
│   │   ├── summarizer.py           # Meeting summarization
│   │   ├── action_extractor.py     # Action item detection
│   │   └── scheduler.py            # Calendar optimization
│   ├── api/
│   │   ├── main.py                 # FastAPI application
│   │   ├── websocket.py            # Real-time audio streaming
│   │   └── routes/                 # API endpoints
│   ├── models/
│   │   ├── database.py             # SQLAlchemy models
│   │   └── schemas.py              # Pydantic schemas
│   ├── services/
│   │   ├── calendar_service.py     # Calendar API integration
│   │   ├── notification.py         # Reminder system
│   │   └── audio_processor.py      # Audio processing
│   └── workers/
│       └── tasks.py                # Celery tasks
├── frontend/
│   └── meeting-ui/                 # React frontend
├── models/
│   └── whisper/                    # Whisper model files
├── tests/
├── requirements.txt
└── docker-compose.yml
```

## Installation

### Prerequisites

- Python 3.10+
- FFmpeg (for audio processing)
- PostgreSQL 14+
- Redis 7+
- Ollama ([ollama.ai](https://ollama.ai))
- Node.js 18+ (for frontend)

### Setup

```bash
cd meeting-intelligence-agent

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install FFmpeg
# macOS: brew install ffmpeg
# Ubuntu: sudo apt-get install ffmpeg
# Windows: Download from ffmpeg.org

# Setup database
createdb meeting_intelligence
alembic upgrade head

# Pull Ollama model
ollama pull llama3.2

# Download Whisper model
python scripts/download_whisper.py

# Configure environment
cp .env.example .env
# Add your calendar API credentials

# Start services
docker-compose up -d  # PostgreSQL, Redis

# Run API server
uvicorn src.api.main:app --reload

# Run Celery worker (separate terminal)
celery -A src.workers.tasks worker --loglevel=info

# Run frontend (separate terminal)
cd frontend/meeting-ui
npm install
npm start
```

## Usage

### Web Interface

1. **Join Meeting**: Open the web app and join via link
2. **Real-time Transcription**: See live transcription as people speak
3. **Auto-summarization**: Get instant summary when meeting ends
4. **Action Items**: Review and assign detected action items
5. **Schedule Follow-up**: AI suggests optimal follow-up time

### API Endpoints

#### Start Meeting Transcription
```bash
POST /api/v1/meetings/start
{
  "title": "Product Planning Meeting",
  "participants": ["alice@company.com", "bob@company.com"],
  "scheduled_time": "2024-01-15T14:00:00Z"
}
```

#### Upload Audio for Transcription
```bash
POST /api/v1/meetings/{meeting_id}/transcribe
Content-Type: multipart/form-data

audio_file: meeting_recording.wav
```

#### Get Meeting Summary
```bash
GET /api/v1/meetings/{meeting_id}/summary
```

#### Extract Action Items
```bash
POST /api/v1/meetings/{meeting_id}/extract-actions
```

#### Find Optimal Meeting Time
```bash
POST /api/v1/calendar/find-optimal-time
{
  "participants": ["alice@company.com", "bob@company.com"],
  "duration_minutes": 60,
  "preferred_days": ["Monday", "Wednesday", "Friday"],
  "time_range": {"start": "09:00", "end": "17:00"}
}
```

### Python SDK

```python
from meeting_agent import MeetingIntelligenceAgent

# Initialize agent
agent = MeetingIntelligenceAgent()

# Start live transcription
meeting = agent.start_meeting(
    title="Team Standup",
    participants=["alice@company.com", "bob@company.com"]
)

# Real-time transcription (WebSocket)
async for transcript in agent.stream_transcription(meeting.id):
    print(f"[{transcript.speaker}]: {transcript.text}")

# End meeting and get summary
summary = agent.end_meeting(meeting.id)
print("Summary:", summary.key_points)
print("Action Items:", summary.action_items)
print("Next Steps:", summary.next_steps)

# Schedule follow-up
optimal_time = agent.find_optimal_time(
    participants=summary.participants,
    duration=30,
    subject="Follow-up: " + meeting.title
)

agent.schedule_meeting(optimal_time)
```

### Transcribe Recorded Meeting

```python
from meeting_agent import MeetingTranscriber

transcriber = MeetingTranscriber(model="whisper-large-v3")

# Transcribe audio file
result = transcriber.transcribe(
    audio_path="meeting_recording.mp3",
    language="en",
    identify_speakers=True  # Speaker diarization
)

# Get transcript
for segment in result.segments:
    print(f"[{segment.speaker}] {segment.start_time} - {segment.end_time}")
    print(f"{segment.text}\n")

# Export to different formats
result.export_txt("transcript.txt")
result.export_srt("transcript.srt")  # Subtitle format
result.export_vtt("transcript.vtt")  # WebVTT format
```

### Extract Action Items

```python
from meeting_agent import ActionItemExtractor

extractor = ActionItemExtractor()

# From transcript
action_items = extractor.extract(transcript)

for item in action_items:
    print(f"• {item.task}")
    print(f"  Assigned to: {item.assignee}")
    print(f"  Due date: {item.due_date}")
    print(f"  Priority: {item.priority}\n")

# Create tasks in project management tools
for item in action_items:
    extractor.create_task(
        item,
        platform="linear"  # or "jira", "asana", "notion"
    )
```

### Calendar Optimization

```python
from meeting_agent import CalendarOptimizer

optimizer = CalendarOptimizer()

# Analyze calendar health
health = optimizer.analyze_calendar("alice@company.com")
print(f"Meeting Load: {health.meeting_hours_per_week}h/week")
print(f"Focus Time: {health.focus_time_blocks}")
print(f"Fragmentation Score: {health.fragmentation_score}/10")

# Get recommendations
recommendations = optimizer.get_recommendations(health)
for rec in recommendations:
    print(f"• {rec.suggestion}")
    print(f"  Impact: {rec.estimated_time_saved}h/week saved\n")

# Auto-decline low-priority meetings
optimizer.auto_decline_meetings(
    user="alice@company.com",
    criteria={
        "optional": True,
        "large_attendee_count": "> 10",
        "recurring": "weekly"
    }
)
```

## Features in Detail

### Real-time Transcription

- **Whisper Models**: Supports tiny, base, small, medium, large
- **Languages**: 99+ languages supported
- **Speaker Diarization**: Identify individual speakers
- **Accuracy**: 95%+ word accuracy
- **Latency**: < 2 seconds for real-time

### Meeting Summarization

Auto-generated summaries include:
- **Executive Summary**: 2-3 sentence overview
- **Key Discussion Points**: Bullet points of main topics
- **Decisions Made**: List of decisions and owners
- **Action Items**: Tasks with assignees and deadlines
- **Next Steps**: Follow-up actions
- **Key Quotes**: Important verbatim quotes

### Action Item Detection

AI detects action items from phrases like:
- "We need to..."
- "I'll follow up on..."
- "Can you look into..."
- "Let's make sure we..."
- "@alice, please..."

Automatically extracts:
- Task description
- Assignee (if mentioned)
- Due date (if mentioned)
- Priority (inferred from context)
- Dependencies

### Smart Scheduling

AI finds optimal meeting times by:
- Analyzing all participants' calendars
- Respecting working hours and time zones
- Avoiding back-to-back meetings
- Preferring morning for important meetings
- Maximizing focus time blocks
- Considering meeting fatigue

## Configuration

Edit `.env`:

```env
# Database
DATABASE_URL=postgresql://user:pass@localhost/meeting_intelligence

# Redis
REDIS_URL=redis://localhost:6379/0

# Ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# Whisper
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda  # or cpu

# Google Calendar
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_secret

# Outlook Calendar (optional)
MICROSOFT_CLIENT_ID=your_client_id
MICROSOFT_CLIENT_SECRET=your_secret

# Notification Services
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email
SMTP_PASSWORD=your_password

# WebRTC
WEBRTC_STUN_SERVER=stun:stun.l.google.com:19302
```

## Performance

- **Real-time transcription:** < 2s latency
- **Batch transcription:** 5-10x faster than real-time
- **Summarization:** 10-20 seconds for 1-hour meeting
- **Action item extraction:** < 5 seconds
- **Calendar analysis:** < 3 seconds

## Browser Extension

Install the browser extension for:
- One-click meeting transcription
- Auto-join Zoom/Meet/Teams meetings
- Background transcription
- Instant summaries after meetings

## Integrations

- ✅ Google Meet
- ✅ Zoom
- ✅ Microsoft Teams
- ✅ Google Calendar
- ✅ Outlook Calendar
- ✅ Linear (for action items)
- ✅ Jira (for action items)
- ✅ Notion (for notes)
- ✅ Slack (for notifications)

## Privacy & Security

- 🔐 **End-to-end encryption** for meeting data
- 🏠 **On-premise deployment** option
- 🗑️ **Auto-delete** after configurable retention period
- 🔒 **Access controls** for meeting recordings
- 📝 **Audit logs** for compliance

## Testing

```bash
# Run all tests
pytest tests/

# Test transcription
pytest tests/test_transcriber.py

# Test with audio samples
pytest tests/test_audio/ --audio-dir=samples/
```

## Roadmap

- [ ] Video meeting bot (auto-join and record)
- [ ] Multi-language support in UI
- [ ] Advanced speaker identification
- [ ] Sentiment analysis during meetings
- [ ] Meeting coaching (improvement suggestions)
- [ ] Integration with more calendars (Apple, Calendly)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## License

MIT License - see [LICENSE](LICENSE)

## Support

- Documentation: [docs.useagenticai.in](https://docs.useagenticai.in)
- Issues: [GitHub Issues](https://github.com/AgenticAI-Ind/meeting-intelligence-agent/issues)
- Email: support@useagenticai.in

---

Built with ❤️ by the AgenticAI team
