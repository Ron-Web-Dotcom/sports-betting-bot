"""
Edge case tests: missing odds, invalid odds, canceled games, API failures, duplicates.
"""
import pytest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from src.engines.ev_engine import evaluate, american_to_decimal, compute_ev
from src.engines.pick_gate import check as gate_check
from src.workers.settlement_worker import _determine_result, _calculate_pnl


# ── Missing / invalid odds ────────────────────────────────────────────────────

def test_ev_with_no_opponent_odds():
    """evaluate() without opponent_odds should use book implied."""
    result = evaluate(american_odds=-110, projected_prob=0.55)
    assert result is not None
    assert result.no_vig_prob == pytest.approx(result.book_implied, rel=1e-6)


def test_pick_gate_with_empty_odds_dict():
    pick = MagicMock()
    pick.recommendation = "BET"
    pick.ev_pct = 0.10
    pick.reasoning = "A" * 100
    pick.key_factors = ["F1", "F2"]
    pick.confidence_pct = 70.0
    pick.risk_score = 35.0
    pick.units = 3
    pick.sport = "nba"
    pick.bet = "Lakers ML"

    # Empty odds dict — gate should still run without crashing
    result = gate_check(pick, odds_by_book={})
    assert result is not None


def test_american_odds_plus_zero():
    # +0 is same as 0 — should return 2.0
    assert american_to_decimal(0) == pytest.approx(2.0)


def test_american_odds_minus_zero():
    # -0 in Python is still 0
    assert american_to_decimal(-0) == pytest.approx(2.0)


# ── Canceled / postponed settlement ──────────────────────────────────────────

def test_settlement_canceled_game_returns_void():
    pick = MagicMock()
    pick.selection = "Lakers"
    pick.units = 2
    score = {"completed": True, "status": "canceled"}
    result = _determine_result(pick, None, score)
    assert result == "void"


def test_settlement_postponed_game_returns_void():
    pick = MagicMock()
    pick.selection = "Lakers"
    pick.units = 2
    score = {"completed": True, "status": "postponed"}
    result = _determine_result(pick, None, score)
    assert result == "void"


# ── Voided bets P&L ───────────────────────────────────────────────────────────

def test_pnl_for_void_result():
    pick = MagicMock()
    pick.units = 2
    pick.american_odds_at_gen = -110
    pnl = _calculate_pnl(pick, "void")
    assert pnl == 0.0


# ── API downtime simulation ───────────────────────────────────────────────────

_ALL_FETCHER_PATCHES = [
    "src.apis.data_hub._fetch_injuries_espn",
    "src.apis.data_hub._fetch_news_espn",
    "src.apis.data_hub._fetch_scoreboard_espn",
    "src.apis.data_hub._fetch_sharp_action",
    "src.apis.data_hub._fetch_weather",
    "src.apis.data_hub._fetch_trending",
    "src.apis.data_hub._fetch_sofascore_game",
    "src.apis.data_hub._fetch_sportsdataio",
    "src.apis.data_hub._fetch_nba_stats",
    "src.apis.data_hub._fetch_sleeper_injuries",
    "src.apis.data_hub._fetch_rotowire_injuries",
    "src.apis.data_hub._fetch_prizepicks_props",
    "src.apis.data_hub._fetch_underdog_props",
    "src.apis.data_hub._fetch_kalshi_markets",
]


def test_data_hub_all_sources_fail_returns_empty_context():
    """build_game_context should return partial/empty dict when all sources fail."""
    patches = {p: patch(p, side_effect=Exception("timeout")) for p in _ALL_FETCHER_PATCHES}
    with ExitStack() as stack:
        for p in patches.values():
            stack.enter_context(p)
        from src.apis.data_hub import build_game_context
        result = build_game_context("basketball_nba", "Lakers", "Celtics", "2026-06-02T19:00:00Z")
        assert isinstance(result, dict)


def test_data_hub_partial_source_failure_continues():
    """Even if one source fails, context building should continue with others."""
    overrides = {
        "src.apis.data_hub._fetch_news_espn": patch("src.apis.data_hub._fetch_news_espn", side_effect=Exception("ESPN down")),
    }
    defaults = {p: patch(p, return_value=[]) for p in _ALL_FETCHER_PATCHES if p not in overrides}
    with ExitStack() as stack:
        for p in {**defaults, **overrides}.values():
            stack.enter_context(p)
        from src.apis.data_hub import build_game_context
        result = build_game_context("basketball_nba", "Lakers", "Celtics", "2026-06-02T19:00:00Z")
        assert isinstance(result, dict)


# ── Risk engine DB failure ────────────────────────────────────────────────────

def test_risk_engine_daily_units_db_failure_returns_zero():
    from src.engines.risk_engine import _daily_units_used
    with patch("src.db.session.get_db", side_effect=Exception("DB down")):
        result = _daily_units_used()
        assert result == 0.0
