"""Tests for FastAPI endpoints"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.main import app
from src.models.database import Base
from src.models.db import get_db
from src.services.auth import get_current_user
from tests.conftest import admin_override


@pytest.fixture
def client():
    """Test client backed by an isolated in-memory database.

    Tests must never write to the real PostgreSQL database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = admin_override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_root_endpoint(client):
    """Test root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Meeting Intelligence Agent API"


def test_health_check(client):
    """Test health check"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_start_meeting(client):
    """Test starting a meeting"""
    meeting_data = {
        "title": "Test Meeting",
        "participants": ["alice@test.com", "bob@test.com"],
        "duration_minutes": 60
    }

    response = client.post("/api/v1/meetings/start", json=meeting_data)
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Test Meeting"
    assert data["status"] == "in_progress"
    assert "id" in data


def test_find_optimal_time(client):
    """Test finding optimal meeting time"""
    request_data = {
        "participants": ["alice@test.com", "bob@test.com"],
        "duration_minutes": 30
    }

    response = client.post("/api/v1/calendar/find-optimal-time", json=request_data)
    assert response.status_code == 200
    data = response.json()
    assert "optimal_time" in data
    assert "confidence" in data
