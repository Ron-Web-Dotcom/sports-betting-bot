"""Comprehensive settlement worker tests."""
import pytest
from unittest.mock import MagicMock, patch
from src.workers.settlement_worker import _determine_result, _calculate_pnl, _normalize_team_name


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pick(selection="Lakers", units=2, american_odds=-110):
    pick = MagicMock()
    pick.selection = selection
    pick.units = units
    pick.american_odds_at_gen = american_odds
    pick.result = None
    pick.id = 1
    pick.sport = "nba"
    pick.game_id = "game_1"
    pick.recommendation = "BET"
    return pick


# ── _normalize_team_name ──────────────────────────────────────────────────────

def test_normalize_strips_fc():
    assert _normalize_team_name("Arsenal FC") == "arsenal"


def test_normalize_strips_city():
    assert _normalize_team_name("Manchester City") == "manchester"


def test_normalize_strips_united():
    assert _normalize_team_name("Manchester United") == "manchester"


def test_normalize_lowercase():
    assert _normalize_team_name("LAKERS") == "lakers"


def test_normalize_empty_string():
    assert _normalize_team_name("") == ""


# ── _determine_result ─────────────────────────────────────────────────────────

def test_determine_result_exact_match_won():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, "Lakers", {"completed": True})
    assert result == "won"


def test_determine_result_normalized_match_won():
    pick = _make_pick("Manchester City")
    result = _determine_result(pick, "Manchester", {"completed": True})
    assert result == "won"


def test_determine_result_no_match_lost():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, "Celtics", {"completed": True})
    assert result == "lost"


def test_determine_result_push():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, None, {"completed": True, "push": True})
    assert result == "push"


def test_determine_result_canceled_void():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, None, {"completed": True, "status": "canceled"})
    assert result == "void"


def test_determine_result_postponed_void():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, None, {"completed": True, "status": "postponed"})
    assert result == "void"


def test_determine_result_no_winner_none():
    pick = _make_pick("Lakers")
    result = _determine_result(pick, None, {"completed": True})
    assert result is None


# ── _calculate_pnl ────────────────────────────────────────────────────────────

def test_calculate_pnl_won_plus200():
    pick = _make_pick(units=2, american_odds=200)
    pnl = _calculate_pnl(pick, "won")
    # dec = 200/100 + 1 = 3.0; profit = (3-1)*2 = 4.0
    assert pnl == pytest.approx(4.0)


def test_calculate_pnl_won_minus110():
    pick = _make_pick(units=2, american_odds=-110)
    pnl = _calculate_pnl(pick, "won")
    dec = 100 / 110 + 1
    assert pnl == pytest.approx((dec - 1) * 2, rel=1e-3)


def test_calculate_pnl_lost():
    pick = _make_pick(units=2, american_odds=-110)
    pnl = _calculate_pnl(pick, "lost")
    assert pnl == -2.0


def test_calculate_pnl_push():
    pick = _make_pick(units=2, american_odds=-110)
    pnl = _calculate_pnl(pick, "push")
    assert pnl == 0.0


def test_calculate_pnl_void():
    pick = _make_pick(units=2, american_odds=-110)
    pnl = _calculate_pnl(pick, "void")
    assert pnl == 0.0


def test_calculate_pnl_none_odds_defaults_to_minus110():
    pick = _make_pick(units=1, american_odds=None)
    pick.american_odds_at_gen = None
    pnl = _calculate_pnl(pick, "won")
    dec = 100 / 110 + 1
    assert pnl == pytest.approx(dec - 1, rel=1e-3)
