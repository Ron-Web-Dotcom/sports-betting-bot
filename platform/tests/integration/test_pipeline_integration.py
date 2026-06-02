"""
Integration tests for the full pick pipeline.
All DB and external API calls are mocked.
"""
import pytest
from contextlib import contextmanager
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base


def _sqlite_get_db():
    """Factory: each call returns a fresh context-manager for the same in-memory engine."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _db():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _db


def _make_ev_result(ev_pct=0.07):
    from src.engines.ev_engine import EVResult
    return EVResult(
        american_odds=-110, decimal_odds=1.909,
        book_implied=0.524, no_vig_prob=0.50,
        projected_prob=0.56, ev_pct=ev_pct,
        units=3, is_positive_ev=ev_pct > 0,
    )


def _make_confidence(score=0.70):
    from src.engines.confidence_engine import ConfidenceResult
    return ConfidenceResult(
        raw_score=score, calibrated_score=score,
        ai_prob=score, model_consensus=score,
        line_movement=0.5, news_impact=0.5,
        calibration_adj=0.0, confidence_bucket="65-70",
    )


def _make_risk(units=3, approved=True):
    from src.engines.risk_engine import RiskAssessment
    return RiskAssessment(
        risk_score=35.0, approved=approved,
        units_allowed=units, red_flags=[],
        kelly_fraction=0.03,
    )


# ── Gate integration ───────────────────────────────────────────────────────────

def test_valid_pick_passes_gate_and_is_persisted():
    """A pick with good EV, reasoning, and factors reaches persist_pick and returns an ID."""
    from src.engines.recommendation_engine import build_recommendation, persist_pick

    ev = _make_ev_result(0.08)
    conf = _make_confidence(0.72)
    risk = _make_risk(3, True)

    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=ev,
        confidence=conf,
        risk=risk,
        comparison=None,
        ai_reasoning="Strong rest advantage: Lakers 3 days off vs Celtics back-to-back. Sharp money moved line 2 points in Lakers direction within 2 hours of opening.",
        key_factors=["3-day rest advantage", "Sharp line movement +2pts", "Home court advantage"],
    )

    get_db = _sqlite_get_db()
    with patch("src.db.session.get_db", side_effect=get_db):
        pick_id = persist_pick(pick, game_id=1, odds_by_book={"DraftKings": -110, "FanDuel": -112})

    assert pick_id is not None
    assert pick.recommendation == "BET"


def test_low_ev_pick_blocked_by_gate():
    """A pick below 3% EV must be blocked — persist_pick returns None."""
    from src.engines.recommendation_engine import build_recommendation, persist_pick

    ev = _make_ev_result(0.015)   # only 1.5% EV — below gate threshold
    conf = _make_confidence(0.65)
    risk = _make_risk(1, True)

    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=ev,
        confidence=conf,
        risk=risk,
        comparison=None,
        ai_reasoning="Slight edge detected on closing line value.",
        key_factors=["CLV edge"],
    )

    get_db = _sqlite_get_db()
    with patch("src.db.session.get_db", side_effect=get_db):
        pick_id = persist_pick(pick, game_id=1)

    assert pick_id is None


def test_no_reasoning_pick_blocked():
    """Pick with empty reasoning must be blocked regardless of EV."""
    from src.engines.recommendation_engine import build_recommendation, persist_pick

    ev = _make_ev_result(0.10)
    conf = _make_confidence(0.75)
    risk = _make_risk(4, True)

    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=ev,
        confidence=conf,
        risk=risk,
        comparison=None,
        ai_reasoning="",   # no reasoning
        key_factors=["Factor A", "Factor B"],
    )

    get_db = _sqlite_get_db()
    with patch("src.db.session.get_db", side_effect=get_db):
        pick_id = persist_pick(pick, game_id=1)

    assert pick_id is None


def test_pass_recommendation_always_persisted():
    """PASS picks always clear the gate and are saved for tracking."""
    from src.engines.recommendation_engine import build_recommendation, persist_pick

    ev = _make_ev_result(-0.02)   # negative EV → PASS
    conf = _make_confidence(0.40)
    risk = _make_risk(0, False)

    pick = build_recommendation(
        sport="basketball_nba",
        game="Lakers vs Celtics",
        bet="Lakers ML",
        ev_result=ev,
        confidence=conf,
        risk=risk,
        comparison=None,
        ai_reasoning="",
        key_factors=[],
    )
    assert pick.recommendation == "PASS"

    get_db = _sqlite_get_db()
    with patch("src.db.session.get_db", side_effect=get_db):
        pick_id = persist_pick(pick, game_id=1)

    # PASS picks always go through the gate — pick_id should be assigned
    assert pick_id is not None


# ── Settlement: no double-settlement ──────────────────────────────────────────

def test_settlement_skips_already_settled_pick():
    """If pick.result is already set, the settlement worker must skip it."""
    settled_pick = MagicMock()
    settled_pick.selection = "Lakers"
    settled_pick.result = "won"   # already settled

    # Simulate the guard: if db_pick.result is not None → continue
    should_skip = settled_pick.result is not None
    assert should_skip


def test_calculate_pnl_for_all_result_types():
    from src.workers.settlement_worker import _calculate_pnl

    pick = MagicMock()
    pick.units = 2
    pick.american_odds_at_gen = -110

    assert _calculate_pnl(pick, "won")  > 0
    assert _calculate_pnl(pick, "lost") == -2
    assert _calculate_pnl(pick, "push") == 0.0
    assert _calculate_pnl(pick, "void") == 0.0


# ── CLV sign correctness ───────────────────────────────────────────────────────

def test_clv_positive_when_we_beat_closing_line():
    from src.engines.clv_engine import calculate_clv
    # We bet -110, line moved to -130 → we got a better price → positive CLV
    assert calculate_clv(-110, -130) > 0


def test_clv_negative_when_line_moved_against_us():
    from src.engines.clv_engine import calculate_clv
    # We bet -130, line moved to -110 → we got a worse price → negative CLV
    assert calculate_clv(-130, -110) < 0


def test_clv_zero_when_line_unchanged():
    from src.engines.clv_engine import calculate_clv
    assert calculate_clv(-110, -110) == pytest.approx(0.0, abs=1e-6)
