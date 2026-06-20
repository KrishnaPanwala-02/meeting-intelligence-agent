"""Tests for FastAPI endpoints"""

import pytest
from fastapi.testclient import TestClient
from src.api.main import app


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


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
