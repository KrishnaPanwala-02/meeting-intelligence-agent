# Meeting Intelligence Agent

Turns meeting recordings into structured, actionable information and keeps track of the work that follows.

Upload a recording and the backend transcribes it (Whisper), separates the speakers (Pyannote), writes a summary and extracts action items with their owners and deadlines (Ollama + Llama 3.2), and puts meetings and deadlines on an **internal calendar**. Everything runs locally; there are no paid external APIs and no external calendar integration.

## How it works

```
 upload audio ─▶ FastAPI ──▶ PostgreSQL (meetings, transcripts, summaries, action items, users)
                    │
                    └─ POST /process ─▶ Redis ─▶ Celery worker
                                                   1. Whisper     speech to text, timestamps
                                                   2. Pyannote    who spoke when (Speaker 1, 2, …)
                                                   3. Ollama      summary, key points, decisions
                                                   4. Ollama      action items + deadline phrases
                                                   5. Python      deadline phrases → dates (deterministic)
```

Deadlines such as "by Friday" or "within three days" are **never calculated by the LLM**. The model only copies the spoken phrase; Python resolves it against the meeting date. Ambiguous phrases ("soon", "next month") stay unresolved and are reported for review instead of guessed.

| Component | Technology |
|---|---|
| API | FastAPI (Python 3.10) |
| Speech to text | faster-whisper (OpenAI Whisper) |
| Speaker diarization | pyannote.audio 3.1 |
| Summaries / action items | Ollama, `llama3.2` (runs on the host) |
| Background jobs | Celery + Redis |
| Database | PostgreSQL 14 |
| Deployment | Docker Compose |

## Quick start (development)

Prerequisites: Docker Desktop, [Ollama](https://ollama.com) running on the host with `ollama pull llama3.2`, and a Hugging Face token with access to `pyannote/speaker-diarization-3.1` (for speaker separation).

```bash
cp .env.example .env
# edit .env: set JWT_SECRET (python -c "import secrets; print(secrets.token_urlsafe(48))"),
#            ADMIN_EMAILS, HF_TOKEN
docker compose up -d
curl localhost:8000/health/ready        # {"status":"ready", ...}
open http://localhost:8000/docs         # interactive API (use the Authorize button)
```

The first start downloads the Whisper and Pyannote models (a few minutes); they are cached in the `model_cache` volume. Containers reach Ollama at `http://host.docker.internal:11434`.

Development mode mounts the source into the containers and reloads on change. PostgreSQL and Redis are published on `127.0.0.1:5433` and `127.0.0.1:6380` (not the usual ports, so they do not clash with local installs).

## Web app (frontend)

A React + TypeScript single-page app in [`frontend/`](frontend). It needs Node 20+ and the backend running on `localhost:8000`.

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173  (API calls are proxied to localhost:8000)
```

Create an account on the sign-up page (use the e-mail listed in `ADMIN_EMAILS` to see legacy meetings), then:

| Screen | What it does |
|---|---|
| **Dashboard** | Open and overdue tasks, upcoming meetings, recent recordings, and a warning if the AI engine (Ollama) is not running |
| **New meeting** | Drag-and-drop audio upload with live background processing, or schedule a meeting with a "find a free time" assistant and conflict handling |
| **Meeting** | Processing progress; summary, key points and decisions; a speaker-coloured transcript that plays and seeks the recording, with search; editable action items; Markdown export; reschedule, cancel and delete |
| **My tasks** | Every action item across your meetings, grouped by urgency (overdue, today, this week, later, no deadline) |
| **Calendar** | Month view of meetings and task deadlines with filters and a day agenda |
| **Settings** | Change password, see and sign out your devices, system status |

Light and dark themes follow your system setting (toggle in the sidebar). Each browser tab keeps its own sign-in (so two tabs can be signed in as different people), renewed automatically; a reload keeps you signed in, closing the tab signs you out. A duplicated tab shares the session and follows its token rotations. Record an **online meeting** (Google Meet, Zoom or Teams open in a browser tab) with **New meeting → Online meeting**: in Chrome or Edge, choose the meeting tab and tick “Also share tab audio”; the tab's sound is mixed with your microphone (the tab alone would miss your own voice), live meters show both, and a warning appears if no meeting audio is heard. It needs consent from everyone in the meeting (a tick the recorder must give, saved with the meeting), works only in Chromium browsers, and does not cover the Zoom/Teams desktop apps (upload their recordings instead). Use headphones to avoid echo. Record meetings straight from the microphone with **New meeting → Record now** (browser recordings are WebM/MP4; they are converted automatically for speaker analysis).

```bash
npm test               # unit and component tests (Vitest)
npm run build          # production build in frontend/dist (served with `npm run preview`)
npm run e2e            # real-browser tests (Playwright); see below
```

Browser tests create users and meetings, so run them against a **disposable** backend, never your real one:

```bash
API_HOST_PORT=18000 REGISTER_LIMIT_PER_HOUR=1000 EXPENSIVE_LIMIT_PER_HOUR=1000 REFRESH_LIMIT_PER_HOUR=5000 \
  docker compose -p mia_ui -f docker-compose.yml -f docker-compose.prod.yml up -d --build
cd frontend && npx playwright install chromium
VITE_BACKEND_URL=http://localhost:18000 npm run e2e
docker compose -p mia_ui -f docker-compose.yml -f docker-compose.prod.yml down -v   # clean up
```

## Production deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

The production override runs the code baked into the image (no bind mount, no `--reload`), does not publish PostgreSQL or Redis at all, turns the API docs off (`ENABLE_DOCS=true` to re-enable) and keeps temporary files in tmpfs. All services have health checks and `restart: unless-stopped`; the API and worker wait for healthy PostgreSQL and Redis. `.env` is excluded from the image by `.dockerignore`; secrets are passed as runtime environment variables only.

Put a TLS-terminating reverse proxy in front of port 8000: the API itself speaks plain HTTP.

**Verify a deployment** from scratch (disposable containers, volumes and ports; your real data is untouched):

```bash
make verify-deploy
```

## Using the API

Interactive documentation is at `/docs` (when enabled). Every `/api/v1/*` route requires a bearer token.

```bash
# 1. account + tokens
curl -X POST localhost:8000/auth/register -H 'Content-Type: application/json' \
     -d '{"email":"me@example.com","password":"a long passphrase"}'
TOKEN=$(curl -s -X POST localhost:8000/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"me@example.com","password":"a long passphrase"}' | jq -r .access_token)

# 2. upload a recording and process it in the background
MID=$(curl -s -H "Authorization: Bearer $TOKEN" -F audio_file=@meeting.wav \
     -F title="Weekly sync" -F participants="bob@example.com" \
     localhost:8000/api/v1/meetings/upload | jq -r .meeting_id)
JOB=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     localhost:8000/api/v1/meetings/$MID/process | jq -r .job_id)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/jobs/$JOB      # state, progress, per-step results

# 3. read the results
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/meetings/$MID/transcript
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/meetings/$MID/summary
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/meetings/$MID/actions
curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/v1/calendar/me@example.com/events
```

### Endpoints

| Area | Endpoints |
|---|---|
| Accounts | `POST /auth/register` · `login` · `refresh` · `logout` · `logout-all` · `change-password` · `forgot-password` · `reset-password` · `GET /auth/me` · `GET /auth/sessions` · `DELETE /auth/sessions/{id}` |
| Meetings | `POST /api/v1/meetings/upload` (audio) · `schedule` · `start` · `GET /api/v1/meetings` · `GET/PUT/DELETE /api/v1/meetings/{id}` · `POST …/cancel` · `…/end` · `GET …/{id}/audio` |
| Processing | `POST …/{id}/process` (background: transcribe → summarize → extract) · `GET /api/v1/jobs/{job_id}` · `GET …/{id}/jobs/latest` · synchronous `POST …/transcribe` · `…/summarize` · `…/extract-actions` |
| Results | `GET …/{id}/transcript` · `…/summary` · `…/actions` |
| Action items | `POST/GET/PUT/DELETE …/{id}/actions[/{action_id}]` · `PATCH …/actions/{action_id}?status=` · `GET /api/v1/actions` (all your tasks, with filters) |
| Speakers & corrections | `GET/PUT …/{id}/speakers` (name a speaker; remembers the voice) · `POST …/speakers/analyze` · `PATCH …/{id}/transcript/segments/{seg}` · `POST …/{id}/transcript/replace` · `GET/PATCH/DELETE /api/v1/voice-profiles[/{id}]` · `GET /api/v1/people` (members to pick from; changing a named speaker corrects a wrong match, and `revoke_previous` removes the wrong person's access) |
| Ask | `POST /api/v1/ask` `{question, meeting_id?, history?}` → `{answer, sources, searched}`. Answers come from meetings you can read, written by the local Ollama model, citing `[M1]`… sources. Counts against the hourly expensive-operation limit. |
| Notifications | `GET /api/v1/notifications` · `POST …/notifications/read` · `DELETE …/notifications/{id}` (new tasks, shared meetings, deadline and meeting reminders, comments, deadline requests; an e-mail copy is opt-in per user via `PATCH /auth/me`) |
| Task discussion | `GET …/actions/{id}/thread` · `POST …/comments` · `DELETE …/comments/{cid}` · `POST …/extension` (the assignee asks for more time) · `POST …/extension/{rid}/decision` (the owner approves or declines) |
| Repeating meetings & time zones | `POST /api/v1/meetings/schedule` accepts `timezone`, `repeat` (`daily`/`weekdays`/`weekly`/`biweekly`/`monthly`) with `repeat_count` or `repeat_until`; `POST …/{id}/cancel?scope=following`; `GET …/{id}/series`; `PATCH /auth/me` sets your time zone |
| Folders & memory | `GET/POST /api/v1/folders` · `GET/PATCH/DELETE /api/v1/folders/{id}` (a folder with its meetings in order, each with its summary) · `PUT /api/v1/meetings/{id}/folder` (move one in or out) · upload and schedule accept `folder_id` or `new_folder` · `GET /api/v1/meetings?folder_id=` · `POST /api/v1/ask` and `/ask/history` take `folder_id`. A meeting in a folder is summarised knowing the earlier ones (their summaries, decisions and open tasks): the summary gets a `follow_up` of up to three lines (Follows up / New / Still open). Deleting a folder keeps its meetings |
| Your voice | `POST /api/v1/voice-profiles/me` (a short recording of yourself, so you are named automatically; every member's own voice is recognised in any meeting, and they are given access automatically) |
| Search & insight | `GET /api/v1/search?q=` (titles, summaries, tasks, transcript lines; `"quotes"` for a phrase) · `GET /api/v1/analytics?days=` (talk time, task completion, weekly trends) · `GET/DELETE /api/v1/ask/history[?meeting_id=]` (your saved Ask conversation) |
| Export & sharing | `GET /api/v1/export/calendar.ics` (meetings + your task deadlines, for Google/Apple/Outlook calendars) · `GET …/{id}/calendar.ics` · `POST …/{id}/send-summary` (e-mail the summary and each person's tasks to participants; needs `EMAIL_ENABLED` + SMTP) · `GET /api/v1/features` |
| Calendar | `GET /api/v1/calendar/{email}/events` (filters: `start_date`, `end_date`, `event_type`, `status`, `meeting_id`, `max_results`) · `POST …/calendar/find-optimal-time` · `GET …/calendar/{email}/health` |
| Operations | `GET /health` (liveness) · `GET /health/ready` (database, Redis, Ollama) |

### Access model

- You can **read** a meeting if you own it or are listed as a participant; only the **owner** can modify it. Anyone else gets `404`.
- Users listed in `ADMIN_EMAILS` can see everything, including legacy meetings that predate ownership.
- The calendar and health endpoints only serve your own `user_email`.
- Access tokens last 15 minutes. Use the rotating refresh token (`/auth/refresh`, 14 days, 30-day maximum session). Reusing an already-used refresh token revokes that session. Logout, password change and reset take effect immediately.
- Password reset sends an e-mail when `EMAIL_ENABLED=true` and SMTP is configured. Without e-mail, an administrator issues a single-use token:
  `docker compose exec api python -m src.cli reset-token user@example.com`

## Configuration

Set in `.env` (see `.env.example`). Docker Compose passes them through.

| Variable | Purpose | Default |
|---|---|---|
| `JWT_SECRET` | Token signing key, ≥ 32 random characters. **Required**; the API refuses to start without it. | – |
| `ADMIN_EMAILS` | Comma-separated administrators | empty |
| `HF_TOKEN` | Hugging Face token for Pyannote | – |
| `OLLAMA_HOST`, `OLLAMA_MODEL` | Local LLM | `http://host.docker.internal:11434`, `llama3.2` |
| `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` | Transcription | `base`, `cpu`, `int8` |
| `CORS_ORIGINS` | Allowed browser origins | `localhost:3000`, `localhost:5173` |
| `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS`, `SESSION_MAX_DAYS` | Token lifetimes | 15, 14, 30 |
| `ALLOW_REGISTRATION` | Allow self-service sign-up | `true` |
| `EXPENSIVE_LIMIT_PER_HOUR` | Per-user cap on upload / processing calls | 30 |
| `REGISTER_LIMIT_PER_HOUR`, `REFRESH_LIMIT_PER_HOUR` | Per-IP caps on sign-ups and token refreshes | 10, 600 |
| `MAX_AUDIO_SIZE_MB` | Upload size limit | 200 |
| `EMAIL_ENABLED`, `SMTP_*`, `PASSWORD_RESET_URL` | Password-reset e-mail | disabled |
| `REMINDERS_ENABLED`, `REMINDER_SCAN_SECONDS` | Background deadline and meeting reminders (and e-mail copies for people who opted in) | on, every 300 s |
| `VOICE_MATCH_SCOPE` | `all`: a meeting is checked against every member's saved voice; `participants`: only the owner's voices and the people listed on it | all |
| `VOICE_CONFIRM_MIN`, `VOICE_MATCH_THRESHOLD` | A voice at least this similar to a saved one is offered as "Is this …?"; at least the second value, it is named automatically | 0.5 / 0.6 |
| `APP_URL` | Base address used for links in e-mails | `http://localhost:5173` |
| `ENABLE_DOCS`, `ENABLE_GOOGLE_OAUTH` | API docs; legacy Google OAuth routes (off; the calendar is internal) | `true`, `false` |
| `CELERY_CONCURRENCY` | Worker processes (each loads Whisper; mind memory) | 1 |

## Testing

```bash
make test               # full suite in the api container, coverage gate 80%
make test-fast          # no real services needed
make test-integration   # real PostgreSQL, Ollama, Whisper, Pyannote
make verify-deploy      # clean production deployment + end-to-end workflow
```

The suite covers: unit tests (deadline resolution, summarizer, scheduling, auth), API tests over HTTP with real audio and fake AI agents, Celery task tests (retries, locking, failure handling), security tests (authorization matrix, token attacks, default-deny on every route), and integration tests on a scratch PostgreSQL database (schema migration, JSON/foreign-key behaviour and race conditions: simultaneous bookings, duplicate action items, registrations and refresh-token reuse). Tests never touch the application database or the real Redis. Integration tests skip themselves when a service is unavailable.

## Operations notes

- **Health:** `/health/ready` returns 503 when PostgreSQL or Redis is down and `degraded` when only Ollama is down.
- **Background jobs:** job state is in Redis (kept 7 days); results are in PostgreSQL. A job whose worker is hard-killed is redelivered after about 2 h 10 min. Retries use exponential backoff for transient failures (e.g. Ollama unavailable).
- **Schema:** tables are created at startup and new columns are added automatically (`init_db`). There is no Alembic migration history yet.
- **Backups:** the data lives in the `postgres_data` and `audio_data` Docker volumes; back both up.
- **Logs:** JSON-free, one line per event with a request ID (`X-Request-ID` header); secrets and tokens are never logged.

## Known limitations


- Participants can read but not modify a meeting.
- All times are naive and treated as one timezone (inputs with an offset are converted to UTC).
- The PostgreSQL password in `docker-compose.yml` is a development default; Redis has no password (neither is published on the host in production mode).
- Containers run as root.

## License

See [LICENSE](LICENSE). Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).
