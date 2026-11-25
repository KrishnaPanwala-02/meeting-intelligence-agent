# Study Guide — Meeting Intelligence Agent (for tomorrow's presentation)

How to use this: read Part 1 (the story) and Part 9 (cheat sheet) tonight, skim Parts 2–6 for concepts, rehearse Part 7 (demo)
and Part 8 (questions) out loud. The full technical reference is in `PROJECT_SUMMARY.md`.

---

## PART 1 — The story in 60 seconds (memorise this)

"Meetings create decisions and promises that people forget. Our **Meeting Intelligence Agent** takes a meeting **audio
recording** and automatically produces a **speaker-labelled transcript**, an **executive summary**, **decisions**, and
**action items with owners and deadlines**, and puts the deadlines on a **calendar**. It **recognises team members by their
voice**, so nobody has to type attendees' e-mails. You can **ask questions** about your meetings, **search** them, and group
them in **folders that remember earlier meetings**. Everything runs **locally** on one laptop with **free open-source AI** —
no cloud, no paid APIs, so confidential meetings stay private."

**Three words to hammer:** *Local. Free. Private.*

---

## PART 2 — The big picture (draw this on the board)

```
 Audio ─► [1 Speech-to-Text] ─┐
                              ├─► Transcript with speakers ─► [2 Summary] ─► [3 Action items] ─► Calendar/Tasks
 Audio ─► [1b Who spoke when]─┘          (Llama 3.2 3B)         (Llama 3.2 3B)
          + voice recognition
```

Components: **React app** (what the user sees) → **FastAPI** (the server) → **PostgreSQL** (stores data) + **Redis** (job
queue) → **Celery worker** (does the heavy AI work) → **Ollama** (runs the LLM). Docker runs API/DB/Redis; the **worker runs
directly on the Mac to use the Apple GPU**.

Why that split? Docker on a Mac is a Linux virtual machine and **cannot use the Mac's GPU**. So the AI worker runs natively.

---

## PART 3 — Concepts you must be able to explain (simple language)

### 3.1 Speech-to-text — Whisper / faster-whisper
- **Whisper** (OpenAI, open source) converts speech to text. **faster-whisper** is a faster re-implementation (CTranslate2).
- We use the **"base"** model, **int8** quantised, on CPU: small and quick, a bit less accurate than larger models.
- It gives **word timestamps** (needed to decide who said which word) and **VAD** (voice activity detection — skips silence).
- *If asked "why base?"* → speed on a laptop; bigger models are more accurate but much slower. Future work: a larger model.

### 3.2 Speaker diarization — pyannote 3.1
- Diarization answers **"who spoke when?"** It outputs time ranges per anonymous speaker (SPEAKER_00, SPEAKER_01…).
- Steps inside: detect speech → cut into small windows → compute a **voice embedding** per window → **cluster** similar
  embeddings → each cluster = one speaker.
- We match the speech-to-text words to speaker turns by **time overlap**. If one sentence spans a speaker change, we split it.

### 3.3 Voice embedding & cosine similarity (the heart of "recognising people")
- An **embedding** is a list of numbers (256 numbers here) that represents how a voice sounds. Same person → similar numbers.
- **Cosine similarity** compares two embeddings: 1.0 = identical direction, 0 = unrelated. Formula: `(a·b) / (|a||b|)`.
- Each member records ~15 s at sign-up; we store the embedding (a **voice profile**).
- In a meeting, each speaker's embedding is compared with every stored profile:
  - **≥ 0.60 → auto-named**; **0.50–0.60 → "Is this X?" (ask the user)**; below that stays "Speaker N".
  - Each person/speaker is used at most once (best pairs first) so two people never get the same name.
- Once recognised, the person's **e-mail is added automatically** to the meeting participants → they can log in and see it.

### 3.4 Why we added "turn-by-turn matching" (great story for questions)
- Problem: in a 20-person test the diarizer only found **9 speakers** — it **merged similar voices** (clusters were too coarse).
- Fix: also compute an embedding for **each speaking turn** (≥1.2 s) and match it directly to saved voices.
- A second problem: voices with several samples scored high against everybody ("hubs"). So scores are **z-normalised per
  person** across the recording (how unusual is this score for this person?), plus a margin over the runner-up.
- Result: **9 groups → 26 labels, 17 of 20 saved voices matched**. Cost: ~2 seconds.
- Honest limit: very short turns (<~1 s) and overlapping speech can't be identified → they stay "Speaker N".

### 3.5 The LLM — Llama 3.2 3B via Ollama
- **LLM** = large language model that generates text. **Llama 3.2 3B** = Meta's 3-billion-parameter model — small enough to
  run on a laptop. **Ollama** = a local server that runs it; our code calls it over HTTP.
- **Temperature 0** → deterministic, least creative (we want facts, not inventions).
- **Structured output (JSON schema):** we tell Ollama the exact JSON shape (fields/types) and it must follow it. Much more
  reliable than "please return JSON".
- Tasks given to the LLM: executive summary; key points/decisions/next steps/quotes; action items; folder follow-up; Q&A.

### 3.6 Fighting hallucination (they WILL ask)
1. Prompt says "use only information in the transcript; never invent".
2. Temperature 0.
3. **Key quotes are verified** to appear word-for-word in the transcript, otherwise dropped.
4. **Deadlines are not computed by the LLM**: it copies the phrase ("by Friday"); **our code** converts it to a date using the
   meeting date.
5. Empty results are allowed ("no decisions") so the model isn't forced to make something up.
6. UI shows warnings (e.g. "follow-up can be wrong"), and users can edit transcripts and tasks.

### 3.7 Action-item extraction
Per item: **task, assignee, deadline phrase, priority**. Assignee text is matched to users by name/e-mail so tasks appear in
**My tasks**. Only the assignee can change a task's status (owner can for unclaimed tasks; admin for any). Pattern-matching
(regex) is a fallback if the LLM is down.

### 3.8 Folders with memory
- User makes a **folder**; meetings in it are numbered **Meeting 1, 2, 3** by date.
- When Meeting 3 is processed, we build a **memory text** from Meetings 1 & 2: their summaries (≤450 chars), decisions and
  still-open tasks, fitted to **4,000 characters** (drop oldest first). A separate LLM call writes a **follow-up**: how this
  meeting continues the earlier ones. Stored with the summary and shown as a card.
- It is **prompt-based memory** (context injection), not retraining the model.

### 3.9 Ask (question answering) — a simple RAG
- **RAG = Retrieval-Augmented Generation**: first *retrieve* relevant text, then let the LLM answer using it.
- We retrieve by the question's words (then recency) from only the meetings the user may read, build a compact tagged
  context `[M1] [M2]…`, and instruct the model to answer only from it, cite sources, and say "not found" if absent.
- No vector database (kept simple/local). Future work: embeddings-based retrieval.

### 3.10 Background jobs — Celery + Redis
- Processing takes minutes, so the API doesn't wait: it puts a **job** in **Redis** (message queue); a **Celery worker**
  picks it up. The UI polls job progress (three step cards).
- Steps are **idempotent** (safe to repeat) and failures **retry automatically** with delays (30 s, 60 s…). A per-meeting
  **lock** prevents duplicate processing.

### 3.11 Security basics
- Passwords: **scrypt** hash with per-user salt (never stored in plain text).
- **JWT access token** (15 min) + **refresh token** (14 days, rotating) per device; "sign out everywhere".
- **Rate limiting** on expensive/login/register endpoints; upload validation (type, size, safe filenames).
- **Authorization:** owner/admin full access, participants read, folders private.
- Secrets live in `.env` (never committed). The app refuses to start without a strong `JWT_SECRET`.

### 3.12 GPU / performance
- Apple **MPS** (Metal Performance Shaders) = PyTorch's GPU backend on Apple silicon. pyannote runs on it (51 s vs much slower on CPU).
- **Concurrency:** Whisper and diarization run **at the same time** in two threads → total ≈ the slower of the two.
- Result: a 3 min 36 s recording went from **>11 min (unfinished)** to **≈157 s**. Remaining time is mostly the LLM
  (summary ≈40 s, tasks ≈60 s).

---

## PART 4 — Features list (so you can say "we also have…")

Upload or **record in browser** • **import audio from device** • schedule meetings (incl. recurring) • processing progress •
speaker panel with **audio clips** and corrections • editable transcript (fix a word everywhere) • summary / decisions /
next steps / quotes • action items with deadlines & priority • task threads + **deadline-extension requests** • **internal
calendar** + conflict check + **find free slot** • `.ics` export • notifications & reminders (30 min before meetings, due
today/tomorrow) • e-mail summary (if SMTP configured) • **search** • **Ask** • **analytics** (who talks, who finishes tasks) •
**folders with memory** • participant **dropdown** of registered members • per-device sessions • dark UI.

---

## PART 5 — Technology table (one line each)

| Tech | Why we chose it |
|---|---|
| faster-whisper | free, accurate, fast local speech recognition |
| pyannote 3.1 | state-of-the-art open diarization + voice embeddings |
| Ollama + Llama 3.2 3B | local LLM, no API cost, privacy |
| FastAPI | fast, typed, auto-validated Python API |
| PostgreSQL + SQLAlchemy | reliable relational data, joins for permissions |
| Redis + Celery | queue for long AI jobs, job status, locks |
| React + TypeScript + Vite | modern typed SPA, fast dev |
| Docker Compose | one-command reproducible stack |
| pytest / Vitest | 625+ backend and 409 frontend tests |

---

## PART 6 — Numbers to remember

- **~77** API endpoints • **13** database tables (users, sessions, tokens, meetings, folders, transcripts, summaries,
  action items, voice profiles, notifications, comments, ask history, calendar accounts).
- Models: Whisper *base*, pyannote *3.1*, Llama *3.2 3B*; embeddings **256-dim**.
- Thresholds: **0.60** auto-name, **0.50** "is this X?".
- Speed: **>11 min → ~157 s** (3 m 36 s recording). GPU speaker analysis **51 s**, Whisper **38 s**, in parallel.
- Tests: **625 passed + 9 skipped** (backend), **409 passed** (frontend).
- Access token **15 min**, refresh **14 days**. Memory text **4,000 chars**. Recurrence up to **52** occurrences.
- Trial: **20 members** with saved voices. 20-person hard test: **9 → 26 labels, 17/20 voices matched**; earlier test **19/20** named.
- Hardware: Apple **M3, 8 cores, 16 GB**.

---

## PART 7 — Live demo script (≈5 minutes) and backup plan

**Before you start (30 min earlier):** run `docker compose up -d`, `make host-worker` (keep that terminal open), start
`ollama serve`, start the frontend, check `curl localhost:8000/health/ready` shows database/redis/ollama ok. Open the app
and **log in first**. **Pre-process one meeting** so you have finished results to show if live processing is slow.

1. **Register screen:** point out "Your voice (required)" — record or import (say: "this is how we recognise people").
2. **New meeting:** upload the 3-min audio, choose/create a **folder**, show the **participants dropdown** of members.
3. **Click Process:** show the progress card (3 steps). *While it runs*, explain the architecture diagram (30–60 s).
4. **Open the finished meeting:** Summary → Decisions/Next steps; Transcript with **named speakers**; click an
   unknown speaker to show **audio clips + "Sounds like X"**, then **edit** a wrong name.
5. **Action items:** owners and deadlines; open **Tasks** / **Calendar** to show deadlines appear; export `.ics`.
6. **Folder:** open the folder timeline (Meeting 1, 2, 3) and the **follow-up card**.
7. **Ask:** "What did Jai agree to do?" → answer with citations. Then **Search** a keyword.
8. Close on **privacy**: "all of this ran on this laptop, offline."

**Backup plan:** if live processing is slow/fails, say "this takes ~2–3 minutes on a laptop, here is one I processed
earlier", and show the finished meeting. Have **screenshots** of every step saved on your desktop. If Ollama stalls, run
`ollama run llama3.2` once to warm the model before presenting.

---

## PART 8 — Likely questions and good answers

**Why local instead of ChatGPT/cloud APIs?** Privacy of confidential meetings, zero cost, works offline, no rate limits.
Trade-off: smaller models, slightly lower accuracy.

**How accurate is it?** Transcription is good for clear speech; names can be misheard (we added edit/replace). Speaker
recognition named 19/20 in one test; in a hard test with 1–2 sentences per person it matched 17/20 voices. Short or
overlapping speech is the weak spot, which is why users can correct names.

**How does voice recognition work?** Voice → 256-number embedding → cosine similarity against saved profiles → threshold
0.6 auto-name, 0.5 confirm. Each person used once per meeting.

**What if two people sound alike?** The diarizer can merge them. We added per-turn matching; remaining mistakes can be
corrected in the speaker panel (name + e-mail), and the correction can also revoke the wrongly granted access.

**What if someone isn't registered?** They stay "Speaker N"; the user names them once and (optionally) saves the voice.

**How do you stop the LLM from making things up?** See 3.6 — temperature 0, transcript-only prompts, quote verification,
code (not LLM) resolves dates, schema-constrained JSON, editable output, UI warnings.

**Why did the summary fail with "malformed data" and how did you fix it?** The 3B model sometimes rambled until its length
limit, giving cut-off JSON. We added a repeat penalty, length caps, **salvaging the complete items from truncated JSON**,
one shorter retry, and a longer timeout. Extraction on the real meeting dropped from a 3-minute failure to ~45 s.

**Why 9 speakers instead of 20?** Diarization clusters similar voices together, so brief speakers merge. We match each turn
to the saved voices to separate them.

**Why does Docker not use the GPU?** Docker on macOS runs a Linux VM with no Metal access, so the AI worker runs natively;
API/DB/Redis stay in Docker.

**Why Celery/Redis?** AI processing takes minutes; a queue keeps the API responsive, supports retries, progress and locks.

**How do folders "remember"?** We inject the earlier meetings' summaries, decisions and open tasks (≤4,000 chars) into the
prompt for the new meeting. It isn't training; it is context.

**Is this RAG? Why no vector DB?** Yes, a simple RAG using keyword retrieval; adequate for personal-scale data, simpler and
fully local. Vector search is future work.

**How are tasks assigned to the right user?** The assignee text is matched to user name/e-mail (first name used); only the
assignee can mark progress.

**How do you handle relative dates like "next Friday"?** The model copies the phrase; our deterministic resolver turns it
into a date from the meeting's date and the user's time zone.

**Is it secure?** Hashed passwords (scrypt), JWT + rotating refresh tokens, per-device sessions, rate limits, upload
validation, ownership-based access, secrets in `.env`. Designed for local use, not public hosting.

**Does it integrate with Google/Outlook calendar?** No — the calendar is internal (built from our database); we provide
one-way `.ics` export. Two-way sync is future work.

**How did you test it?** 625 backend and 409 frontend automated tests, type-check/build, plus real-recording trials with the
20 members.

**Scalability?** Built for one machine; the design (queue + stateless API + Postgres) could scale out with more workers
and a GPU server, but that's beyond this scope.

**Biggest limitations?** Small models (accuracy), overlapping/short speech, similar voices, English-focused, internal
calendar only, local deployment.

**What would you improve next?** Bigger Whisper/LLM, speaker-count hint, live transcription, two-way calendar sync,
multilingual support, embedding-based retrieval.

**What was the hardest part?** (pick one) Getting reliable structured output from a small LLM, or recognising many
similar voices in one meeting. Describe the problem → your fix → the measured result.

**What did *you* contribute?** Be ready to name the parts you can explain best (e.g., voice recognition, folders memory,
pipeline performance) — see Part 3.

---

## PART 9 — One-page cheat sheet

- **Pipeline:** Audio → Whisper (text) ∥ pyannote (speakers) → match voices → Llama summary → Llama action items → DB → calendar.
- **Voice match:** embedding(256) + cosine; 0.6 auto, 0.5 ask; turn-by-turn + z-norm for merged voices.
- **Anti-hallucination:** temp 0, transcript-only, quote check, code resolves dates, JSON schema.
- **Architecture:** React → FastAPI → Postgres / Redis → Celery worker (Mac, GPU) → Ollama.
- **Speed:** 11+ min → 157 s (GPU + parallel).
- **Unique features:** auto-recognise & auto-share by voice, folder memory, listen-before-naming, Ask with citations, local & free.
- **Honest limits:** small models, short/overlapping speech, similar voices, internal calendar.

## PART 10 — Glossary
**ASR** speech recognition • **Diarization** who-spoke-when • **Embedding** numeric voice/text fingerprint • **Cosine
similarity** angle-based similarity • **LLM** large language model • **Quantisation (int8)** smaller/faster model numbers •
**VAD** voice activity detection • **RAG** retrieval-augmented generation • **JWT** signed login token • **scrypt** password
hashing • **Celery** task queue • **Redis** in-memory store • **MPS** Apple GPU backend • **Idempotent** safe to run twice •
**JSON schema** required output shape • **Hallucination** model inventing facts • **z-score** score relative to its usual
spread.

## PART 11 — Night-before checklist
- [ ] Everything starts (Docker, Ollama, Mac worker, frontend) and `/health/ready` is ok
- [ ] One finished meeting + one folder with 2–3 meetings ready
- [ ] Screenshots of each demo step saved
- [ ] Laptop charged, notifications off, internet not needed
- [ ] Rehearsed the 60-second story and the demo in under 5 minutes
- [ ] Know your answers to: accuracy, hallucination, why local, 9-vs-20 speakers, limitations
