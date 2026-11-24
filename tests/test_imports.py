"""Every module the server, the worker and the CLI start from must import in a fresh interpreter.

Other tests import modules in a convenient order, which can hide a circular import that only
appears in the order a real process uses (this once left the API unable to start).
"""

import os
import subprocess
import sys

import pytest

ENTRY_POINTS = [
    "src.api.main",              # uvicorn src.api.main:app
    "src.workers.tasks",         # celery -A src.workers.tasks
    "src.workers",
    "src.services.pipeline",
    "src.services.voices",
    "src.services.notifications",
    "src.services.reminders",
    "src.services.task_thread",
    "src.services.assignments",
    "src.services.auth",
    "src.cli",
]


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_imports_cleanly_on_its_own(module):
    env = {**os.environ, "JWT_SECRET": "x" * 48, "PYTHONWARNINGS": "ignore"}
    done = subprocess.run([sys.executable, "-W", "ignore", "-c", f"import {module}"], capture_output=True, text=True,
                          timeout=240, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert done.returncode == 0, f"import {module} failed:\n{done.stderr[-1500:]}"
