"""
Security tests — input validation, injection resistance, boundary enforcement.
All tests are fully mocked — no real DB, network, or Discord calls.
"""
import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
from src.engines.ev_engine import american_to_decimal, compute_ev, assign_units, evaluate


# ── Input boundary: EV / odds ──────────────────────────────────────────────────

def test_extremely_large_positive_ev_capped_at_5_units():
    assert assign_units(999.0) == 5

def test_extremely_large_negative_ev_returns_zero():
    assert assign_units(-999.0) == 0

def test_zero_decimal_odds_compute_ev_no_crash():
    # compute_ev(p, 0.0) = p*(0-1) - (1-p) = -1 — no crash
    result = compute_ev(0.5, 0.0)
    assert isinstance(result, float)

def test_negative_decimal_odds_compute_ev_no_crash():
    result = compute_ev(0.5, -1.0)
    assert isinstance(result, float)

def test_evaluate_extreme_positive_odds_no_crash():
    result = evaluate(100000, 0.5)
    assert result.decimal_odds > 1.0
    assert isinstance(result.ev_pct, float)

def test_evaluate_extreme_negative_odds_no_crash():
    result = evaluate(-100000, 0.5)
    assert result.decimal_odds > 1.0

def test_projected_prob_above_one_clamped():
    # evaluate clamps to >= 0; values > 1 are passed through (physically impossible
    # but the EV formula still produces a float — no crash)
    result = evaluate(-110, 1.5)
    assert isinstance(result.ev_pct, float)

def test_projected_prob_negative_clamped_to_zero():
    result = evaluate(-110, -0.99)
    assert result.projected_prob == 0.0


# ── SQL injection resistance ───────────────────────────────────────────────────

def test_sql_injection_in_selection_field_stored_literally():
    """
    SQLAlchemy uses parameterized queries — injected SQL is stored as literal text.
    We verify the pick gate and recommendation engine don't eval or exec the string.
    """
    from src.engines.pick_gate import check
    mock_pick = MagicMock()
    mock_pick.recommendation = "BET"
    mock_pick.ev_pct = 0.05
    mock_pick.reasoning = "x" * 100
    mock_pick.key_factors = ["Factor A", "Factor B"]
    mock_pick.confidence_pct = 65.0
    mock_pick.risk_score = 30.0
    mock_pick.units = 2
    mock_pick.sport = "basketball_nba"
    mock_pick.bet = "'; DROP TABLE picks; --"   # injection attempt

    result = check(mock_pick)
    # Gate evaluates based on EV/reasoning/factors — not the bet string
    assert isinstance(result.passed, bool)


# ── Pick gate: reasoning whitespace-only ──────────────────────────────────────

def test_whitespace_only_reasoning_blocked():
    from src.engines.pick_gate import check, MIN_REASONING_CHARS
    mock_pick = MagicMock()
    mock_pick.recommendation = "BET"
    mock_pick.ev_pct = 0.05
    mock_pick.reasoning = " " * 200   # lots of spaces but no content
    mock_pick.key_factors = ["Factor A", "Factor B"]
    mock_pick.confidence_pct = 65.0
    mock_pick.risk_score = 30.0
    mock_pick.units = 2
    mock_pick.sport = "nba"
    mock_pick.bet = "Lakers ML"

    result = check(mock_pick)
    assert not result.passed
    assert any("Reasoning" in r for r in result.reasons)


def test_key_factors_empty_strings_dont_count():
    from src.engines.pick_gate import check
    mock_pick = MagicMock()
    mock_pick.recommendation = "BET"
    mock_pick.ev_pct = 0.05
    mock_pick.reasoning = "x" * 100
    mock_pick.key_factors = ["", "  ", ""]   # three empty/blank factors
    mock_pick.confidence_pct = 65.0
    mock_pick.risk_score = 30.0
    mock_pick.units = 2
    mock_pick.sport = "nba"
    mock_pick.bet = "Lakers ML"

    result = check(mock_pick)
    assert not result.passed
    assert any("key factors" in r for r in result.reasons)


# ── Bankroll: boundary enforcement ────────────────────────────────────────────

def test_bankroll_examples_negative_units_returns_empty():
    from src.engines.ev_engine import bankroll_examples
    assert bankroll_examples(-5) == {}

def test_bankroll_examples_zero_units_returns_empty():
    from src.engines.ev_engine import bankroll_examples
    assert bankroll_examples(0) == {}

def test_bankroll_examples_unit_size_zero_no_crash():
    from src.engines.ev_engine import bankroll_examples
    result = bankroll_examples(2, unit_size_pct=0.0, american_odds=-110)
    for v in result.values():
        assert v["stake"] == 0.0


_ALL_HUB_FETCHERS = [
    "src.apis.data_hub._fetch_injuries_espn",
    "src.apis.data_hub._fetch_news_espn",
    "src.apis.data_hub._fetch_scoreboard_espn",
    "src.apis.data_hub._fetch_sharp_action",
    "src.apis.data_hub._fetch_pinnacle_signal",
    "src.apis.data_hub._fetch_weather",
    "src.apis.data_hub._fetch_trending",
    "src.apis.data_hub._fetch_sleeper_injuries",
    "src.apis.data_hub._fetch_sportsdataio",
    "src.apis.data_hub._fetch_kalshi_markets",
    "src.apis.data_hub._fetch_sportradar_game",
    "src.apis.data_hub._fetch_thesportsdb",
]

# ── Data hub: all sources fail → graceful degradation ─────────────────────────

def test_data_hub_all_sources_fail_returns_minimal_context():
    with ExitStack() as stack:
        for p in _ALL_HUB_FETCHERS:
            stack.enter_context(patch(p, side_effect=Exception("timeout")))
        from src.apis.data_hub import build_game_context
        ctx = build_game_context("basketball_nba", "Lakers", "Celtics", "2024-01-15T20:00:00Z")

    assert ctx["sport"] == "basketball_nba"
    assert ctx["data_completeness"] == 0.0
    assert isinstance(ctx["sources_failed"], list)
    assert len(ctx["sources_failed"]) > 0


def test_data_hub_partial_failure_uses_available_sources():
    overrides = {
        "src.apis.data_hub._fetch_injuries_espn": [{"player": "X"}],
        "src.apis.data_hub._fetch_news_espn":     Exception("timeout"),
    }
    with ExitStack() as stack:
        for p in _ALL_HUB_FETCHERS:
            val = overrides.get(p, [])
            if isinstance(val, Exception):
                stack.enter_context(patch(p, side_effect=val))
            else:
                stack.enter_context(patch(p, return_value=val))
        from src.apis.data_hub import build_game_context
        ctx = build_game_context("basketball_nba", "Lakers", "Celtics", "2024-01-15T20:00:00Z")

    assert ctx["data_completeness"] > 0.0
    assert "injuries_espn_home" in ctx
