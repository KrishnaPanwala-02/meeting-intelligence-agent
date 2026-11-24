"""
Processing-job state, kept in Redis.

A job is one run of one or more pipeline steps for a meeting. The record is
what the API shows to users; results themselves are saved in PostgreSQL.
A per-meeting lock prevents two jobs from processing the same meeting at once.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis

JOB_TTL_SECONDS = 7 * 24 * 3600
LOCK_TTL_SECONDS = 2 * 3600 + 600      # a bit above the task time limit

_client: Optional[redis.Redis] = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


def set_client(new_client) -> None:
    """Replace the Redis client (used by tests)."""
    global _client
    _client = new_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _latest_key(meeting_id: str) -> str:
    return f"job:latest:{meeting_id}"


def _lock_key(meeting_id: str) -> str:
    return f"job:lock:{meeting_id}"


def acquire_lock(meeting_id: str, job_id: str) -> Optional[str]:
    """Take the meeting lock. Returns None on success, else the holder's job id."""
    r = client()
    if r.set(_lock_key(meeting_id), job_id, nx=True, ex=LOCK_TTL_SECONDS):
        return None
    holder = r.get(_lock_key(meeting_id))
    if holder == job_id:        # the same job re-entering (retry)
        r.expire(_lock_key(meeting_id), LOCK_TTL_SECONDS)
        return None
    return holder


def release_lock(meeting_id: str, job_id: str) -> None:
    r = client()
    if r.get(_lock_key(meeting_id)) == job_id:
        r.delete(_lock_key(meeting_id))


def create_job(meeting_id: str, steps: List[str], options: Dict[str, Any],
               job_id: Optional[str] = None) -> Dict[str, Any]:
    job = {
        "job_id": job_id or str(uuid.uuid4()),
        "meeting_id": meeting_id,
        "state": "queued",
        "steps": [{"name": s, "state": "pending", "error": None,
                   "result": None} for s in steps],
        "options": options,
        "progress_percent": 0,
        "attempts": 0,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_job(job)
    client().set(_latest_key(meeting_id), job["job_id"], ex=JOB_TTL_SECONDS)
    return job


def save_job(job: Dict[str, Any]) -> None:
    done = sum(1 for s in job["steps"] if s["state"] in ("succeeded", "skipped"))
    job["progress_percent"] = int(100 * done / max(1, len(job["steps"])))
    job["updated_at"] = _now()
    client().set(_job_key(job["job_id"]), json.dumps(job), ex=JOB_TTL_SECONDS)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    raw = client().get(_job_key(job_id))
    return json.loads(raw) if raw else None


def latest_job_id(meeting_id: str) -> Optional[str]:
    return client().get(_latest_key(meeting_id))


def active_job_for(meeting_id: str) -> Optional[Dict[str, Any]]:
    """The running/queued job holding this meeting's lock, if any."""
    holder = client().get(_lock_key(meeting_id))
    return get_job(holder) if holder else None


def reclaim_if_stale(meeting_id: str) -> bool:
    """
    Free the meeting lock if its job is missing or already finished (for
    example after a worker crash). Returns True if a stale lock was removed.
    """
    r = client()
    holder = r.get(_lock_key(meeting_id))
    if not holder:
        return False
    job = get_job(holder)
    if job is None or job["state"] in ("succeeded", "failed"):
        r.delete(_lock_key(meeting_id))
        return True
    return False
