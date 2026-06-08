import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
import src.api.main as api_main


@pytest.fixture
def client():
    from src.api.main import app
    api_main._API_KEY = ""  # disable auth for tests
    return TestClient(app)


def test_discuss_analyze(client):
    with patch("src.api.routers.discussion.handle_command", return_value="Great analysis"):
        response = client.post("/discuss/", json={"command": "analyze", "args": "Lakers vs Celtics"})
    assert response.status_code == 200
    assert response.json()["response"] == "Great analysis"


def test_discuss_invalid_command(client):
    response = client.post("/discuss/", json={"command": "invalid_cmd", "args": "test"})
    assert response.status_code == 400


def test_discuss_all_valid_commands(client):
    commands = ["analyze", "player", "team", "parlay", "explain", "why", "odds", "bankroll"]
    for cmd in commands:
        with patch("src.api.routers.discussion.handle_command", return_value="ok"):
            r = client.post("/discuss/", json={"command": cmd, "args": "test"})
        assert r.status_code == 200, f"Command {cmd} failed"
