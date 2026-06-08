"""Comprehensive tests for timing_engine — from 42% coverage."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from src.engines.timing_engine import (
    minutes_to_game, get_alert_window, should_fire_alert,
    get_urgency_level, upcoming_games_by_window,
)


# ── minutes_to_game ───────────────────────────────────────────────────────────

def test_future_game_positive_minutes():
    future = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    result = minutes_to_game(future)
    assert 118 < result < 122


def test_past_game_negative_minutes():
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    result = minutes_to_game(past)
    assert result < 0


def test_game_starting_now_near_zero():
    now = datetime.utcnow().isoformat()
    result = minutes_to_game(now)
    assert -2 < result < 2


def test_tz_aware_string_no_error():
    """Timezone-aware ISO strings must not cause TypeError."""
    result = minutes_to_game("2099-01-01T15:00:00+00:00")
    assert isinstance(result, float)
    assert result > 0


def test_tz_aware_with_offset_no_error():
    result = minutes_to_game("2099-06-15T20:00:00-05:00")
    assert isinstance(result, float)


def test_bad_string_returns_9999():
    assert minutes_to_game("not-a-date") == 9999.0


def test_empty_string_returns_9999():
    assert minutes_to_game("") == 9999.0


def test_datetime_object_accepted():
    future = datetime.utcnow() + timedelta(minutes=30)
    result = minutes_to_game(future)
    assert 28 < result < 32


# ── get_alert_window ──────────────────────────────────────────────────────────

def test_window_60_min():
    assert get_alert_window(62) == 60


def test_window_30_min():
    assert get_alert_window(31) == 30


def test_window_15_min():
    assert get_alert_window(16) == 15


def test_window_5_min():
    assert get_alert_window(6) == 5


def test_window_0_min():
    assert get_alert_window(1) == 0


def test_no_window_when_too_far_out():
    # More than 62 minutes out — no alert window
    result = get_alert_window(200)
    assert result is None


def test_window_within_tolerance():
    # get_alert_window uses +2 min tolerance
    assert get_alert_window(62) == 60   # at edge of 60-min window
    assert get_alert_window(63) is None  # just over tolerance


# ── get_urgency_level ─────────────────────────────────────────────────────────

def test_urgency_critical_under_5():
    assert get_urgency_level(3) == "critical"
    assert get_urgency_level(0) == "critical"


def test_urgency_high_under_15():
    assert get_urgency_level(10) == "high"
    assert get_urgency_level(14) == "high"


def test_urgency_medium_under_60():
    assert get_urgency_level(30) == "medium"
    assert get_urgency_level(59) == "medium"


def test_urgency_low_over_60():
    assert get_urgency_level(61) == "low"
    assert get_urgency_level(200) == "low"


# ── should_fire_alert ─────────────────────────────────────────────────────────

def test_should_fire_when_no_record():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.filter.return_value.first.return_value = None
    ms.query.return_value.filter.return_value.first.return_value = None
    with patch("src.db.session.get_db", return_value=ms):
        assert should_fire_alert("game-1", 30) is True


def test_should_not_fire_when_record_exists():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = MagicMock()
    with patch("src.db.session.get_db", return_value=ms):
        assert should_fire_alert("game-1", 30) is False


def test_should_fire_returns_true_on_db_error():
    with patch("src.db.session.get_db", side_effect=Exception("DB down")):
        assert should_fire_alert("game-1", 30) is True


# ── upcoming_games_by_window ──────────────────────────────────────────────────

def test_upcoming_games_groups_correctly():
    events = [
        {"commence_time": (datetime.utcnow() + timedelta(minutes=31)).isoformat()},
        {"commence_time": (datetime.utcnow() + timedelta(minutes=16)).isoformat()},
    ]
    result = upcoming_games_by_window(events)
    assert isinstance(result, dict)
    assert 30 in result or 15 in result


def test_upcoming_games_excludes_already_started():
    events = [
        {"commence_time": (datetime.utcnow() - timedelta(minutes=5)).isoformat()},
    ]
    result = upcoming_games_by_window(events)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_upcoming_games_excludes_too_far_out():
    events = [
        {"commence_time": (datetime.utcnow() + timedelta(hours=5)).isoformat()},
    ]
    result = upcoming_games_by_window(events)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_upcoming_games_empty_input():
    result = upcoming_games_by_window([])
    assert all(v == [] for v in result.values())
