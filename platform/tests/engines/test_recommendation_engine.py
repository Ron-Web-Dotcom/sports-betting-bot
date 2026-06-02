import pytest
from src.engines.ev_engine import EVResult
from src.engines.confidence_engine import ConfidenceResult
from src.engines.risk_engine import RiskAssessment
from src.engines.recommendation_engine import build_recommendation, PickRecommendation


def _ev(ev_pct=0.07, american_odds=-110, is_positive=True):
    return EVResult(
        american_odds=american_odds, decimal_odds=1.909,
        book_implied=0.524, no_vig_prob=0.50,
        projected_prob=0.56, ev_pct=ev_pct,
        units=3, is_positive_ev=is_positive,
    )


def _conf(score=0.70):
    return ConfidenceResult(
        raw_score=score, calibrated_score=score,
        ai_prob=score, model_consensus=score,
        line_movement=0.5, news_impact=0.5,
        calibration_adj=0.0, confidence_bucket="65-70",
    )


def _risk(units=3, approved=True):
    return RiskAssessment(
        risk_score=35.0, approved=approved,
        units_allowed=units, red_flags=[],
        kelly_fraction=0.03,
    )


def test_build_recommendation_bet():
    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=_ev(),
        confidence=_conf(),
        risk=_risk(),
        comparison=None,
        ai_reasoning="Strong edge detected.",
        key_factors=["Home court", "Rested"],
    )
    assert isinstance(pick, PickRecommendation)
    assert pick.recommendation == "BET"
    assert pick.units == 3
    assert pick.ev_pct == pytest.approx(0.07)
    assert pick.opportunity_score > 0


def test_build_recommendation_pass_when_risk_not_approved():
    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=_ev(),
        confidence=_conf(),
        risk=_risk(approved=False, units=0),
        comparison=None,
        ai_reasoning="Risk too high.",
        key_factors=[],
    )
    assert pick.recommendation == "PASS"
    assert pick.units == 0


def test_opportunity_score_is_bounded():
    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=_ev(ev_pct=0.50),
        confidence=_conf(0.99),
        risk=_risk(units=5),
        comparison=None,
        ai_reasoning="Elite play.",
        key_factors=[],
    )
    assert 0 <= pick.opportunity_score <= 100
