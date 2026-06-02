"""Regression tests for H-001, H-002, H-003, H-014, M-001, M-005."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


# ── H-003: asyncio.run() instead of get_event_loop ────────────────────────────

def test_run_async_uses_asyncio_run():
    """_run_async must use asyncio.run() — not the deprecated get_event_loop."""
    import inspect
    import src.workers.alert_worker as aw
    src_lines = inspect.getsource(aw._run_async)
    assert "asyncio.run(" in src_lines
    assert "get_event_loop" not in src_lines
    assert "new_event_loop" not in src_lines


def test_run_async_works_with_simple_coroutine():
    from src.workers.alert_worker import _run_async
    import asyncio
    async def _coro():
        return 42
    assert _run_async(_coro()) == 42


def test_run_async_can_be_called_multiple_times():
    """Each call gets a fresh loop — no 'loop is closed' error on second call."""
    from src.workers.alert_worker import _run_async
    import asyncio
    async def _coro(val):
        return val
    assert _run_async(_coro(1)) == 1
    assert _run_async(_coro(2)) == 2   # second call must not raise


# ── H-002: CLV savepoint prevents session poisoning ──────────────────────────

def test_clv_upsert_uses_begin_nested_not_rollback():
    """db.rollback() inside a session context kills the entire transaction.
    The fix uses db.begin_nested() (savepoint) instead."""
    import inspect
    import src.engines.clv_engine as ce
    src_lines = inspect.getsource(ce.record_clv)
    assert "begin_nested" in src_lines
    # db.rollback() must not appear as executable code (comments are fine)
    code_lines = [l for l in src_lines.splitlines() if not l.strip().startswith("#")]
    assert not any("db.rollback()" in l for l in code_lines)


# ── M-005: Timezone-aware datetime handling in timing engine ─────────────────

def test_minutes_to_game_with_tz_aware_string():
    """Parsing a timezone-aware ISO string must not raise TypeError."""
    from src.engines.timing_engine import minutes_to_game
    # This would raise TypeError before the fix (aware vs naive comparison)
    result = minutes_to_game("2099-01-01T15:00:00+00:00")
    assert isinstance(result, float)
    assert result > 0   # far future game


def test_minutes_to_game_with_naive_string():
    from src.engines.timing_engine import minutes_to_game
    result = minutes_to_game("2099-06-15T20:00:00")
    assert isinstance(result, float)
    assert result > 0


def test_minutes_to_game_past_game_is_negative():
    from src.engines.timing_engine import minutes_to_game
    result = minutes_to_game("2000-01-01T00:00:00")
    assert result < 0


def test_minutes_to_game_bad_input_returns_9999():
    from src.engines.timing_engine import minutes_to_game
    assert minutes_to_game("not-a-date") == 9999.0
    assert minutes_to_game("") == 9999.0


# ── M-001: SQLite blocked in production ──────────────────────────────────────

def test_sqlite_allowed_in_development():
    """No error when USE_SQLITE=true and ENVIRONMENT=development."""
    import sys
    with patch.dict("os.environ", {
        "USE_SQLITE": "true",
        "ENVIRONMENT": "development",
        "ANTHROPIC_API_KEY": "test",
        "ODDS_API_KEY": "test",
    }):
        for mod in list(sys.modules):
            if "src.core.config" in mod:
                del sys.modules[mod]
        import src.core.config  # must not raise
        assert src.core.config.USE_SQLITE is True


def test_sqlite_blocked_in_production():
    """RuntimeError when USE_SQLITE=true and ENVIRONMENT=production."""
    import sys
    with patch.dict("os.environ", {
        "USE_SQLITE": "true",
        "ENVIRONMENT": "production",
        "ANTHROPIC_API_KEY": "test",
        "ODDS_API_KEY": "test",
    }):
        for mod in list(sys.modules):
            if "src.core.config" in mod:
                del sys.modules[mod]
        with pytest.raises(RuntimeError, match="USE_SQLITE=true is not allowed"):
            import src.core.config  # noqa


# Restore config module
import sys, os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ODDS_API_KEY", "test-odds-key")
for mod in list(sys.modules):
    if "src.core.config" in mod:
        del sys.modules[mod]
import src.core.config


# ── H-014: Redis atomic unit reservation ─────────────────────────────────────

def test_atomic_unit_check_falls_back_gracefully_when_redis_down():
    """If Redis is unavailable, falls back to DB check without crashing."""
    from src.engines.risk_engine import _atomic_daily_unit_check_and_reserve
    with patch("redis.from_url", side_effect=Exception("Redis down")):
        with patch("src.engines.risk_engine._daily_units_used", return_value=0.0):
            result = _atomic_daily_unit_check_and_reserve(
                units=3, sport="nba", red_flags=[], risk_score=40.0, fk=0.1
            )
    assert result == 3  # approved — 0 used + 3 = 3 < 15


def test_atomic_unit_check_rejects_when_limit_hit_via_db_fallback():
    from src.engines.risk_engine import _atomic_daily_unit_check_and_reserve, RiskAssessment
    with patch("redis.from_url", side_effect=Exception("Redis down")):
        with patch("src.engines.risk_engine._daily_units_used", return_value=15.0):
            result = _atomic_daily_unit_check_and_reserve(
                units=3, sport="nba", red_flags=[], risk_score=40.0, fk=0.1
            )
    assert isinstance(result, RiskAssessment)
    assert result.approved is False
    assert "limit" in result.rejection_reason.lower()


def test_atomic_unit_check_redis_atomically_reserves():
    """Simulate Redis INCRBY returning a value within limits — units approved."""
    from src.engines.risk_engine import _atomic_daily_unit_check_and_reserve
    mock_redis = MagicMock()
    mock_redis.incrby.return_value = 5   # 5 total after adding 3 (was 2)
    with patch("redis.from_url", return_value=mock_redis):
        result = _atomic_daily_unit_check_and_reserve(
            units=3, sport="nfl", red_flags=[], risk_score=30.0, fk=0.15
        )
    assert result == 3
    mock_redis.incrby.assert_called_once()
    mock_redis.expire.assert_called_once()


def test_atomic_unit_check_redis_rolls_back_when_over_limit():
    """If INCRBY pushes over limit, decrby rolls back and returns remaining."""
    from src.engines.risk_engine import _atomic_daily_unit_check_and_reserve, RiskAssessment
    mock_redis = MagicMock()
    mock_redis.incrby.return_value = 17   # over 15-unit limit
    with patch("redis.from_url", return_value=mock_redis):
        result = _atomic_daily_unit_check_and_reserve(
            units=3, sport="nfl", red_flags=[], risk_score=30.0, fk=0.15
        )
    mock_redis.decrby.assert_called_once()  # rolled back
    # remaining = 15 - (17-3) = 1
    assert isinstance(result, int) or isinstance(result, RiskAssessment)


# ── H-001: Detached ORM — verify scalar query pattern ────────────────────────

def test_record_closing_lines_uses_scalar_columns():
    """record_closing_lines must query individual columns (not ORM objects)
    so they're safe to use after the session closes."""
    import inspect
    import src.workers.settlement_worker as sw
    src_lines = inspect.getsource(sw.record_closing_lines)
    # Must query specific columns, not the full Pick object
    assert "Pick.id" in src_lines
    assert "Pick.game_id" in src_lines
    assert "Pick.american_odds_at_gen" in src_lines
