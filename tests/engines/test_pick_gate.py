"""
Tests for the pick gate.

Every test that expects a block must assert the specific reason —
vague failures are as bad as no gate at all.
"""
import pytest
from src.engines.pick_gate import check, GateResult, MIN_EV_PCT_GATE, MIN_REASONING_CHARS


def _pick(
    ev_pct=0.05,
    reasoning="Lakers have a significant rest advantage with 3 days off vs Celtics on a back-to-back. Sharp money moved the line 2.5 points.",
    key_factors=("Rest advantage", "Sharp line movement"),
    confidence_pct=65.0,
    risk_score=30.0,
    units=2,
    recommendation="BET",
):
    from unittest.mock import MagicMock
    p = MagicMock()
    p.recommendation = recommendation
    p.ev_pct = ev_pct
    p.reasoning = reasoning
    p.key_factors = list(key_factors)
    p.confidence_pct = confidence_pct
    p.risk_score = risk_score
    p.units = units
    p.sport = "basketball_nba"
    p.bet = "Lakers ML"
    return p


def test_valid_pick_passes():
    result = check(_pick())
    assert result.passed
    assert result.reasons == []


def test_pass_recommendation_always_clears():
    """PASS picks must never be blocked — logging them is always correct."""
    p = _pick(ev_pct=0.0, reasoning="", key_factors=[], recommendation="PASS")
    result = check(p)
    assert result.passed


def test_low_ev_blocked():
    result = check(_pick(ev_pct=0.02))
    assert not result.passed
    assert any("EV too low" in r for r in result.reasons)


def test_ev_at_exact_floor_passes():
    result = check(_pick(ev_pct=MIN_EV_PCT_GATE))
    assert result.passed


def test_short_reasoning_blocked():
    result = check(_pick(reasoning="Good bet."))
    assert not result.passed
    assert any("Reasoning too short" in r for r in result.reasons)


def test_reasoning_at_min_length_passes():
    reasoning = "x" * MIN_REASONING_CHARS
    result = check(_pick(reasoning=reasoning))
    assert result.passed


def test_no_key_factors_blocked():
    result = check(_pick(key_factors=[]))
    assert not result.passed
    assert any("key factors" in r for r in result.reasons)


def test_one_key_factor_blocked():
    result = check(_pick(key_factors=["Only one factor"]))
    assert not result.passed
    assert any("key factors" in r for r in result.reasons)


def test_two_key_factors_passes():
    result = check(_pick(key_factors=["Factor A", "Factor B"]))
    assert result.passed


def test_low_confidence_blocked():
    result = check(_pick(confidence_pct=40.0))
    assert not result.passed
    assert any("Confidence too low" in r for r in result.reasons)


def test_high_risk_score_blocked():
    result = check(_pick(risk_score=80.0))
    assert not result.passed
    assert any("Risk score too high" in r for r in result.reasons)


def test_zero_units_blocked():
    result = check(_pick(units=0))
    assert not result.passed
    assert any("Units" in r for r in result.reasons)


def test_multiple_failures_all_reported():
    """All failure reasons should be returned, not just the first."""
    result = check(_pick(ev_pct=0.01, reasoning="short", key_factors=[]))
    assert not result.passed
    assert len(result.reasons) >= 3


def test_marginal_ev_generates_warning():
    """Pick passes but warns when EV is barely above floor."""
    result = check(_pick(ev_pct=0.035))
    assert result.passed
    assert any("marginal" in w for w in result.warnings)


def test_gate_result_is_dataclass():
    result = check(_pick())
    assert isinstance(result, GateResult)
    assert isinstance(result.passed, bool)
    assert isinstance(result.reasons, list)
    assert isinstance(result.warnings, list)


def test_multi_book_check_with_sufficient_books():
    odds = {"DraftKings": -110, "FanDuel": -112, "BetMGM": -108}
    result = check(_pick(), odds_by_book=odds)
    assert result.passed


def test_no_odds_book_generates_warning_not_block():
    """Missing book data should warn, not block — data may be unavailable."""
    result = check(_pick(), odds_by_book=None)
    assert result.passed
    assert any("cross-check skipped" in w for w in result.warnings)
