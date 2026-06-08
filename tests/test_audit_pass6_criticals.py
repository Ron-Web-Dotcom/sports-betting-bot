"""
Regression tests for bugs found in QA audit pass 6.

Bugs fixed:
  B1 - portfolio /bankroll-history crashed: wrong BankrollSnapshot field names
  B2 - ai_engine._call_json silently dropped markdown-fenced JSON responses
  B3 - No API authentication — all endpoints publicly accessible
  B4 - generate_picks() had no idempotency guard (duplicate picks on concurrent runs)
  B5 - news_engine.get_recent_injuries missing (ImportError in picks_worker at runtime)
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# ── B1: portfolio bankroll-history uses correct field names ─────────────────────

def test_bankroll_history_uses_recorded_at():
    from src.api.routers.portfolio import bankroll_history
    import inspect
    src = inspect.getsource(bankroll_history)
    assert "snapshot_date" not in src, "snapshot_date field removed from BankrollSnapshot"
    assert "recorded_at" in src


def test_bankroll_history_uses_balance():
    from src.api.routers.portfolio import bankroll_history
    import inspect
    src = inspect.getsource(bankroll_history)
    assert "daily_pnl" not in src, "daily_pnl field removed from BankrollSnapshot"
    assert "balance" in src


def test_bankroll_history_endpoint_returns_list():
    from src.api.main import app
    client = TestClient(app)

    row = MagicMock()
    row.recorded_at = None
    row.balance = 1000.0
    row.units_total = 50.0
    row.note = "auto"

    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.order_by.return_value.all.return_value = [row]

    with patch("src.db.session.get_db", return_value=ms):
        resp = client.get("/portfolio/bankroll-history")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["balance"] == 1000.0
    assert "units_total" in data[0]


# ── B2: ai_engine strips markdown fences from JSON responses ────────────────────

def _openai_resp(text: str) -> MagicMock:
    """Build a mock that matches openai.ChatCompletion response shape."""
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_call_json_strips_markdown_fences():
    from src.engines import ai_engine
    fenced = '```json\n{"should_bet": true, "recommendation": "BET"}\n```'

    with patch.object(ai_engine._client.chat.completions, "create",
                      return_value=_openai_resp(fenced)):
        result = ai_engine._call_json("prompt", "system")

    assert result is not None
    assert result["should_bet"] is True


def test_call_json_plain_json_still_works():
    from src.engines import ai_engine
    plain = '{"should_bet": false, "recommendation": "PASS"}'

    with patch.object(ai_engine._client.chat.completions, "create",
                      return_value=_openai_resp(plain)):
        result = ai_engine._call_json("prompt", "system")

    assert result is not None
    assert result["recommendation"] == "PASS"


def test_call_json_returns_none_on_invalid_json():
    from src.engines import ai_engine

    with patch.object(ai_engine._client.chat.completions, "create",
                      return_value=_openai_resp("not valid json at all")):
        result = ai_engine._call_json("prompt", "system")

    assert result is None


def test_call_json_returns_none_on_api_error():
    from openai import APIError
    from src.engines import ai_engine

    with patch.object(ai_engine._client.chat.completions, "create",
                      side_effect=APIError("rate limit", request=MagicMock(), body=None)):
        result = ai_engine._call_json("prompt", "system")

    assert result is None


def test_call_json_strips_fence_without_language_tag():
    from src.engines import ai_engine
    fenced = '```\n{"key": "value"}\n```'

    with patch.object(ai_engine._client.chat.completions, "create",
                      return_value=_openai_resp(fenced)):
        result = ai_engine._call_json("prompt", "system")

    assert result is not None
    assert result["key"] == "value"


# ── B3: API authentication middleware ──────────────────────────────────────────

def test_api_key_middleware_exists_in_source():
    import inspect
    from src.api import main
    src = inspect.getsource(main)
    assert "PLATFORM_API_KEY" in src
    assert "X-API-Key" in src or "x-api-key" in src.lower()


def test_health_endpoint_accessible_without_key():
    """Health endpoint must stay public (for Docker healthchecks)."""
    import os
    from src.api.main import app
    client = TestClient(app)

    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.limit.return_value.all.return_value = []

    with patch.dict(os.environ, {"PLATFORM_API_KEY": "secret123"}), \
         patch("src.db.session.get_db", return_value=ms):
        resp = client.get("/health")

    assert resp.status_code == 200


def test_picks_endpoint_blocked_without_key():
    import os
    from src.api.main import app

    # Need a fresh app instance to pick up the env var set on the middleware
    # — patch the module-level _API_KEY directly
    import src.api.main as api_main
    original = api_main._API_KEY
    try:
        api_main._API_KEY = "secret123"
        client = TestClient(app)

        ms = MagicMock()
        ms.__enter__ = MagicMock(return_value=ms)
        ms.__exit__ = MagicMock(return_value=False)
        ms.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        with patch("src.db.session.get_db", return_value=ms):
            resp = client.get("/picks/")
    finally:
        api_main._API_KEY = original

    assert resp.status_code == 401


def test_picks_endpoint_accessible_with_correct_key():
    import src.api.main as api_main
    from src.api.main import app
    original = api_main._API_KEY
    try:
        api_main._API_KEY = "secret123"
        client = TestClient(app)

        with patch("src.api.routers.picks.get_db") as mock_get_db:
            ms = MagicMock()
            ms.__enter__ = MagicMock(return_value=ms)
            ms.__exit__ = MagicMock(return_value=False)
            ms.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
            mock_get_db.return_value = ms
            resp = client.get("/picks/", headers={"X-API-Key": "secret123"})
    finally:
        api_main._API_KEY = original

    assert resp.status_code == 200


def test_api_key_via_query_param():
    import src.api.main as api_main
    from src.api.main import app
    original = api_main._API_KEY
    try:
        api_main._API_KEY = "mykey"
        client = TestClient(app)

        with patch("src.api.routers.picks.get_db") as mock_get_db:
            ms = MagicMock()
            ms.__enter__ = MagicMock(return_value=ms)
            ms.__exit__ = MagicMock(return_value=False)
            ms.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
            mock_get_db.return_value = ms
            resp = client.get("/picks/?api_key=mykey")
    finally:
        api_main._API_KEY = original

    assert resp.status_code == 200


def test_wrong_key_returns_401():
    import src.api.main as api_main
    from src.api.main import app
    original = api_main._API_KEY
    try:
        api_main._API_KEY = "correct"
        client = TestClient(app)
        resp = client.get("/picks/", headers={"X-API-Key": "wrong"})
    finally:
        api_main._API_KEY = original

    assert resp.status_code == 401


# ── B4: generate_picks idempotency guard ────────────────────────────────────────

def test_generate_picks_has_idempotency_guard():
    import inspect
    from src.workers import picks_worker
    src = inspect.getsource(picks_worker.generate_picks)
    assert "nx=True" in src or "SETNX" in src or "lock" in src.lower()


def test_generate_picks_skips_on_lock_held():
    mock_redis = MagicMock()
    mock_redis.set.return_value = False   # lock already held

    with patch("redis.from_url", return_value=mock_redis):
        from src.workers.picks_worker import generate_picks
        result = generate_picks()

    assert result == {"skipped": True}


# ── B5: get_recent_injuries exists and returns list ─────────────────────────────

def test_get_recent_injuries_exists():
    from src.engines.news_engine import get_recent_injuries
    assert callable(get_recent_injuries)


def test_get_recent_injuries_returns_list():
    from src.engines.news_engine import get_recent_injuries
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    row = MagicMock()
    row.sport = "nba"
    row.affected_players = ["LeBron James"]
    row.affected_teams = ["Lakers"]
    row.detail = "Ankle"
    # filter() with two args still returns a single mock — .all() on that
    ms.query.return_value.filter.return_value.all.return_value = [row]

    with patch("src.db.session.get_db", return_value=ms):
        result = get_recent_injuries()

    assert isinstance(result, list)
    assert result[0]["player_name"] == "LeBron James"
    assert result[0]["team"] == "Lakers"


def test_get_recent_injuries_empty_when_no_records():
    from src.engines.news_engine import get_recent_injuries
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = []

    with patch("src.db.session.get_db", return_value=ms):
        result = get_recent_injuries()

    assert result == []
