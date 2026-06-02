import pytest
from src.engines.risk_engine import assess, RiskAssessment


def _assess(units=3, ev_pct=0.07, confidence=0.65, win_prob=0.55, injuries=0):
    return assess(
        requested_units=units,
        sport="basketball_nba",
        ev_pct=ev_pct,
        confidence=confidence,
        win_prob=win_prob,
        decimal_odds=1.909,
        injury_flags=injuries,
    )


def test_assess_returns_risk_assessment():
    result = _assess()
    assert isinstance(result, RiskAssessment)
    assert 0 <= result.risk_score <= 100
    assert result.units_allowed in range(0, 6)


def test_high_ev_high_confidence_approves():
    result = _assess(units=4, ev_pct=0.12, confidence=0.80)
    assert result.approved
    assert result.units_allowed >= 3


def test_negative_ev_rejects():
    result = _assess(ev_pct=-0.03, units=0)
    assert not result.approved
    assert result.units_allowed == 0


def test_injury_flag_detected():
    result = assess(
        requested_units=3,
        sport="basketball_nba",
        ev_pct=0.07,
        confidence=0.65,
        win_prob=0.55,
        decimal_odds=1.909,
        conflicting_injuries=True,
    )
    assert any("injury" in f.lower() for f in result.red_flags)


def test_zero_units_not_approved():
    result = _assess(units=0, ev_pct=0.00)
    assert result.units_allowed == 0
    assert not result.approved
