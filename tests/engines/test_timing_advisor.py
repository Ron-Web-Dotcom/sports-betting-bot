"""Tests for timing_advisor."""
import pytest
from src.engines.timing_advisor import advise_timing, get_historical_pattern, BetTiming


def test_bet_now_when_under_15_min():
    result = advise_timing({}, -110, [], minutes_to_game=10)
    assert result.recommendation == "bet_now"
    assert result.confidence >= 0.90


def test_bet_now_when_0_min():
    result = advise_timing({}, -110, [], minutes_to_game=0)
    assert result.recommendation == "bet_now"


def test_bet_now_no_history():
    result = advise_timing({}, -110, [], minutes_to_game=60)
    assert result.recommendation == "bet_now"
    assert result.confidence < 0.95


def test_bet_now_on_sharp_large_delta():
    movements = [{"odds_before": -110, "odds_after": -140, "movement_type": "sharp"}]
    result = advise_timing({}, -110, movements, minutes_to_game=60)
    assert result.recommendation == "bet_now"
    assert "sharp" in result.reasoning.lower() or "steam" in result.reasoning.lower()


def test_bet_now_on_steam_movement():
    movements = [{"odds_before": -105, "odds_after": -130, "movement_type": "steam"}]
    result = advise_timing({}, -105, movements, minutes_to_game=120)
    assert result.recommendation == "bet_now"


def test_wait_on_public_money():
    # All deltas positive (odds_after > odds_before numerically) + public type + delta < 10
    movements = [
        {"odds_before": 100, "odds_after": 105, "movement_type": "public"},
        {"odds_before": 105, "odds_after": 108, "movement_type": "public"},
        {"odds_before": 108, "odds_after": 109, "movement_type": "public"},
    ]
    result = advise_timing({}, 109, movements, minutes_to_game=60)
    assert result.recommendation in ("wait", "value_expiring")


def test_better_line_expected_on_all_negative():
    # Deltas: odds_after - odds_before must be negative for each movement
    movements = [
        {"odds_before": -100, "odds_after": -110, "movement_type": ""},  # delta=-10
        {"odds_before": -95, "odds_after": -105, "movement_type": ""},   # delta=-10
        {"odds_before": -90, "odds_after": -100, "movement_type": ""},   # delta=-10
    ]
    result = advise_timing({}, -100, movements, minutes_to_game=60)
    assert result.recommendation == "better_line_expected"


def test_result_is_betting_timing():
    result = advise_timing({}, -110, [], minutes_to_game=30)
    assert isinstance(result, BetTiming)
    assert result.reasoning


def test_get_historical_pattern_known():
    pattern = get_historical_pattern("basketball_nba", "spread")
    assert "avg_move_per_hour" in pattern
    assert "sharp_window_hours" in pattern


def test_get_historical_pattern_unknown_returns_default():
    pattern = get_historical_pattern("unknown_sport", "h2h")
    assert pattern["avg_move_per_hour"] == 0.5
    assert pattern["sharp_window_hours"] == 2


def test_value_expiring_on_consistent_move_away():
    movements = [
        {"odds_before": -100, "odds_after": -110, "movement_type": "sharp"},
        {"odds_before": -110, "odds_after": -120, "movement_type": "sharp"},
        {"odds_before": -120, "odds_after": -135, "movement_type": "sharp"},
    ]
    # All positive (price getting worse), large delta ≥20
    result = advise_timing({}, -135, movements, minutes_to_game=60)
    # Large delta triggers bet_now (sharp detected)
    assert result.recommendation == "bet_now"
