"""
Celery tasks: background processing of meeting recordings.

Pipeline:  transcribe (Whisper + Pyannote) -> summarize (Ollama) ->
           extract_actions (Ollama + deterministic deadline resolution).

Every step is idempotent, so a retried job skips steps that already
succeeded. Transient failures (e.g. Ollama unavailable) are retried with
exponential backoff; permanent failures (e.g. no audio) fail immediately.
"""

import asyncio
import os
from typing import Any, Dict, List

from celery import Celery
from celery.exceptions import Retry
from loguru import logger

from ..models.db import SessionLocal
from ..services import audio_storage, pipeline
from . import jobs

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "meeting_intelligence",
    broker=REDIS_URL,
    backend=REDIS_URL,
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=2 * 3600,
    task_soft_time_limit=2 * 3600 - 300,
    result_expires=jobs.JOB_TTL_SECONDS,
    broker_connection_retry_on_startup=True,
    # visibility_timeout must exceed the task time limit, otherwise Redis
    # redelivers a long-running task while it is still executing.
    broker_transport_options={"max_retries": 2, "socket_timeout": 5,
                              "socket_connect_timeout": 5,
                              "visibility_timeout": 2 * 3600 + 600},
    task_default_retry_delay=30,
    timezone="UTC",
)

MAX_RETRIES = int(os.getenv("PIPELINE_MAX_RETRIES", "3"))
RETRY_BASE_SECONDS = int(os.getenv("PIPELINE_RETRY_BASE_SECONDS", "30"))

# Replaced in tests.
session_factory = SessionLocal


def _run_step(db, meeting, name: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """Run one pipeline step and return a small JSON-safe result summary."""
    if name == "transcribe":
        audio = audio_storage.local_path(meeting.audio_file_path)
        if not audio or not os.path.exists(audio):
            raise pipeline.PipelineError(
                "No stored audio for this meeting. Upload a recording first.", 400
            )
        transcript, result = pipeline.transcribe_step(
            db, meeting, audio,
            language=options.get("language", "en"),
            identify_speakers=options.get("identify_speakers", True),
        )
        return {"transcript_id": transcript.id,
                "word_count": transcript.word_count,
                "speaker_count": transcript.speaker_count,
                "audio_seconds": round(result.duration_seconds, 2)}

    if name == "voices":
        return pipeline.voices_step(db, meeting)

    if name == "summarize":
        summary, existed = asyncio.run(pipeline.summarize_step(
            db, meeting, regenerate=bool(options.get("regenerate_summary"))))
        return {"summary_id": summary.id, "already_existed": existed}

    if name == "extract_actions":
        out = asyncio.run(pipeline.extract_actions_step(db, meeting))
        return {"saved": len(out["saved"]),
                "skipped_duplicates": out["skipped_duplicates"],
                "unresolved_deadlines": len(out["unresolved_deadlines"])}

    raise pipeline.PipelineError(f"Unknown step {name!r}", 400)


def _execute(task, job_id: str, meeting_id: str, steps: List[str],
             options: Dict[str, Any]) -> Dict[str, Any]:
    job = jobs.get_job(job_id) or jobs.create_job(
        meeting_id, steps, options, job_id=job_id)

    holder = jobs.acquire_lock(meeting_id, job_id)
    if holder is not None:
        job.update(state="failed",
                   error=f"Meeting is already being processed by job {holder}")
        jobs.save_job(job)
        logger.warning("Job {} rejected: meeting {} locked by {}",
                       job_id, meeting_id, holder)
        return job

    job["attempts"] = task.request.retries + 1
    job["state"] = "running"
    job["error"] = None
    jobs.save_job(job)

    keep_lock = False
    db = session_factory()
    try:
        try:
            meeting = pipeline.get_meeting(db, meeting_id)
        except pipeline.PipelineError as exc:
            job.update(state="failed", error=exc.message)
            for step in job["steps"]:
                step["state"] = "skipped_due_to_failure"
            jobs.save_job(job)
            return job

        for step in job["steps"]:
            if step["state"] in ("succeeded", "skipped"):
                continue                      # finished before a retry
            step.update(state="running", error=None)
            jobs.save_job(job)
            try:
                step["result"] = _run_step(db, meeting, step["name"], options)
                step["state"] = "succeeded"
                jobs.save_job(job)
            except pipeline.PipelineError as exc:
                db.rollback()
                step.update(state="failed", error=exc.message)
                if exc.retryable and task.request.retries < MAX_RETRIES:
                    delay = RETRY_BASE_SECONDS * (2 ** task.request.retries)
                    step["state"] = "pending"
                    job.update(state="retrying",
                               error=f"{exc.message} (retrying in {delay}s)")
                    jobs.save_job(job)
                    keep_lock = True          # the retry re-enters with the lock
                    logger.warning("Job {} step {} failed, retry in {}s: {}",
                                   job_id, step["name"], delay, exc.message)
                    raise task.retry(exc=exc, countdown=delay,
                                     max_retries=MAX_RETRIES)
                job.update(state="failed", error=exc.message)
                for later in job["steps"]:
                    if later["state"] == "pending":
                        later["state"] = "skipped_due_to_failure"
                jobs.save_job(job)
                logger.error("Job {} failed at {}: {}", job_id,
                             step["name"], exc.message)
                return job
            except Exception as exc:          # unexpected: do not leak internals
                db.rollback()
                logger.exception("Job {} step {} crashed", job_id, step["name"])
                step.update(state="failed", error="Internal error while processing")
                job.update(state="failed", error="Internal error while processing")
                jobs.save_job(job)
                return job

        job.update(state="succeeded", error=None)
        jobs.save_job(job)
        logger.info("Job {} succeeded for meeting {}", job_id, meeting_id)
        return job
    except Retry:
        keep_lock = True
        raise
    finally:
        db.close()
        if not keep_lock:
            jobs.release_lock(meeting_id, job_id)


def _define(name: str, steps: List[str]):
    @celery_app.task(bind=True, name=name, acks_late=True)
    def task(self, job_id: str, meeting_id: str,
             options: Dict[str, Any] | None = None):
        return _execute(self, job_id, meeting_id, steps, options or {})
    return task


process_meeting = _define("process_meeting", list(pipeline.STEPS))
transcribe_audio = _define("transcribe_audio", ["transcribe"])
generate_summary = _define("generate_summary", ["summarize"])
extract_action_items = _define("extract_action_items", ["extract_actions"])
analyze_voices = _define("analyze_voices", ["voices"])

TASKS_BY_STEPS = {
    ("transcribe",): transcribe_audio,
    ("summarize",): generate_summary,
    ("extract_actions",): extract_action_items,
    ("voices",): analyze_voices,
}


def task_for(steps: List[str]):
    """The Celery task that runs exactly these steps, in order."""
    return TASKS_BY_STEPS.get(tuple(steps), process_meeting)
