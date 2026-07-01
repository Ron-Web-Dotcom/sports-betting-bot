"""Comprehensive pick gate edge case tests."""
import pytest
from unittest.mock import MagicMock
from src.engines.pick_gate import check, MIN_EV_PCT_GATE, MIN_CONFIDENCE, MAX_RISK_SCORE


def _make_pick(
    recommendation="BET",
    ev_pct=0.10,
    reasoning="A" * 100,   # 100 chars — above minimum
    key_factors=["Factor 1", "Factor 2"],
    confidence_pct=80.0,
    risk_score=40.0,
    units=3,
):
    pick = MagicMock()
    pick.recommendation = recommendation
    pick.ev_pct = ev_pct
    pick.reasoning = reasoning
    pick.key_factors = key_factors
    pick.confidence_pct = confidence_pct
    pick.risk_score = risk_score
    pick.units = units
    pick.sport = "nba"
    pick.bet = "Lakers ML"
    return pick


# ── PASS picks always allowed ─────────────────────────────────────────────────

def test_pass_recommendation_always_allowed():
    pick = _make_pick(recommendation="PASS", ev_pct=0.0, reasoning="", key_factors=[], units=0)
    result = check(pick)
    assert result.passed is True


# ── EV boundary ───────────────────────────────────────────────────────────────

def test_ev_exactly_at_minimum_passes():
    pick = _make_pick(ev_pct=MIN_EV_PCT_GATE)
    result = check(pick)
    # Should not fail on EV alone
    assert not any("EV too low" in r for r in result.reasons)


def test_ev_just_below_minimum_blocked():
    pick = _make_pick(ev_pct=MIN_EV_PCT_GATE - 0.001)
    result = check(pick)
    assert any("EV too low" in r for r in result.reasons)


# ── Reasoning ─────────────────────────────────────────────────────────────────

def test_empty_reasoning_blocked():
    pick = _make_pick(reasoning="")
    result = check(pick)
    assert any("Reasoning too short" in r for r in result.reasons)


def test_whitespace_only_reasoning_blocked():
    pick = _make_pick(reasoning="   " * 30)   # lots of whitespace
    result = check(pick)
    # strip() makes this 0 chars
    assert any("Reasoning too short" in r for r in result.reasons)


# ── Key factors ───────────────────────────────────────────────────────────────

def test_empty_string_factors_not_counted():
    pick = _make_pick(key_factors=["", ""])
    result = check(pick)
    assert any("key factors" in r.lower() for r in result.reasons)


def test_zero_real_factors_blocked():
    pick = _make_pick(key_factors=["", " "])
    result = check(pick)
    assert any("key factors" in r.lower() for r in result.reasons)


# ── EV boundary 0.03 ──────────────────────────────────────────────────────────

def test_ev_exactly_003_passes_ev_check():
    pick = _make_pick(ev_pct=0.03)
    result = check(pick)
    assert not any("EV too low" in r for r in result.reasons)


def test_ev_0299_blocked():
    pick = _make_pick(ev_pct=0.0299)
    result = check(pick)
    assert any("EV too low" in r for r in result.reasons)


# ── Risk score boundary ───────────────────────────────────────────────────────

def test_risk_score_exactly_75_passes():
    pick = _make_pick(risk_score=75.0)
    result = check(pick)
    assert not any("Risk score" in r for r in result.reasons)


def test_risk_score_75_1_blocked():
    pick = _make_pick(risk_score=75.1)
    result = check(pick)
    assert any("Risk score" in r for r in result.reasons)


# ── Confidence boundary ───────────────────────────────────────────────────────

def test_confidence_exactly_min_passes():
    pick = _make_pick(confidence_pct=MIN_CONFIDENCE * 100)
    result = check(pick)
    assert not any("Confidence too low" in r for r in result.reasons)


def test_confidence_below_min_blocked():
    pick = _make_pick(confidence_pct=(MIN_CONFIDENCE * 100) - 1.0)
    result = check(pick)
    assert any("Confidence too low" in r for r in result.reasons)
