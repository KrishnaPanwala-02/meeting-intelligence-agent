"""
Run the processing worker on this machine instead of in Docker, so speaker analysis can use the
GPU (Apple's Metal on a Mac). Docker on a Mac cannot reach the GPU.

    make host-worker          (or: .venv-host/bin/python scripts/host_worker.py)

Postgres, Redis and the API keep running in Docker; the worker reaches them on their published ports.
First time: make host-env       Stop the Docker worker meanwhile: docker compose stop celery-worker
"""
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
env.update(os.environ)                       # anything already set in the shell wins
# These differ between "inside Docker" (what .env and compose describe) and "on this machine".
host = {
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5433/meeting_intelligence",
    "REDIS_URL": "redis://localhost:6380/0",
    "OLLAMA_HOST": "http://localhost:11434",
    "AUDIO_STORAGE_PATH": str(ROOT / "data" / "audio"),
    "PYTORCH_ENABLE_MPS_FALLBACK": "1",      # an operation the GPU lacks runs on the CPU instead of failing
    "TOKENIZERS_PARALLELISM": "false",
}
for key, value in host.items():
    if key not in os.environ:                # an explicit setting in the shell still wins
        env[key] = value

os.chdir(ROOT)
cmd = [sys.executable, "-m", "celery", "-A", "src.workers.tasks", "worker",
       "--loglevel=info", "--pool=solo", "--concurrency=1"]       # solo: no forking after the GPU is in use
os.execvpe(cmd[0], cmd, env)
