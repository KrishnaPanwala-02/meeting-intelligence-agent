"""Shared test configuration. Tests never touch the real Redis or Postgres."""

import os

# Hard-set (not setdefault): tests must not depend on, or leak, real settings.
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ["ADMIN_EMAILS"] = "tester@example.com"
os.environ["ALLOW_REGISTRATION"] = "true"
os.environ["EXPENSIVE_LIMIT_PER_HOUR"] = "1000"

import pytest

from src.models.database import User
from src.workers import jobs

TEST_ADMIN = User(id="test-admin", email="tester@example.com",
                  password_hash="x", is_active=True)


class FakeRedis:
    """Minimal in-memory Redis (only the commands the app uses)."""

    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        v = self.data.get(key)
        return None if v is None else str(v)

    def delete(self, key):
        self.data.pop(key, None)

    def expire(self, key, seconds):
        return True

    def ping(self):
        return True

    def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]


def admin_override():
    return TEST_ADMIN


@pytest.fixture(autouse=True)
def _isolated_redis():
    jobs.set_client(FakeRedis())
    yield
    jobs.set_client(None)


TEST_WAV = os.path.join(os.path.dirname(__file__), "data", "two_speakers.wav")
