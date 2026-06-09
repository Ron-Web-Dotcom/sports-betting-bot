"""
Engine 7 — Recommendation Engine.

Central orchestrator. Takes all engine outputs and produces a final Pick.
Implements the full output format specified in the master prompt.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.engines.ev_engine import EVResult, bankroll_examples, american_to_decimal
from src.engines.confidence_engine import ConfidenceResult
from src.engines.risk_engine import RiskAssessment
from src.engines.comparison_engine import BookComparison
from src.core.config import DEFAULT_BANKROLL, UNIT_SIZE_PCT
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)


@dataclass
class PickRecommendation:
    # Event
    sport:          str
    game:           str
    bet:            str           # "Lakers ML" / "Over 224.5" / "LeBron 30+ pts"
    market:         str           # h2h | spreads | totals | player_prop
    sportsbook:     str
    odds:           int

    # Verdict
    recommendation: str           # BET | PASS
    confidence_pct: float         # 0-100
    risk_score:     float         # 0-100
    ev_pct:         float
    opportunity_score: float      # 0-100

    # Sizing
    units:          int           # 0-5
    unit_reason:    str
    ev_range:       str
    bankroll_examples: dict       # {100: {stake, profit, return}, ...}

    # Book comparison
    book_comparison_table: str

    # CLV potential
    clv_potential:  float

    # Explanation
    reasoning:      str
    key_factors:    list[str] = field(default_factory=list)
    risk_flags:     list[str] = field(default_factory=list)

    # Model scores
    statistical_score: float = 0.0
    ml_score:          float = 0.0
    market_score:      float = 0.0
    trend_score:       float = 0.0

    # Timestamps
    generated_at:   str = field(default_factory=lambda: et_naive().isoformat())


def _opportunity_score(ev_pct: float, confidence: float, units: int, clv_potential: float) -> float:
    """0-100 composite opportunity score."""
    score = (
        min(ev_pct * 200, 40) +        # EV: 20% EV = max 40 pts
        confidence * 100 * 0.30 +       # Confidence: 30 pts
        units / 5 * 20 +                # Units: 20 pts
        min(clv_potential * 200, 10)    # CLV potential: 10 pts
    )
    return round(min(100.0, score), 1)


def _unit_reason(units: int, ev_pct: float) -> tuple[str, str]:
    """Returns (reason, ev_range)."""
    labels = {
        0: ("No positive EV detected", "< 1%"),
        1: ("Small edge detected",      "1% - 3%"),
        2: ("Good edge — solid play",   "3% - 6%"),
        3: ("Strong edge — high conviction", "6% - 10%"),
        4: ("Very strong edge — elite opportunity", "10% - 15%"),
        5: ("Elite play — maximum conviction", "15%+"),
    }
    label, ev_range = labels.get(units, ("Unknown", "N/A"))
    risk_label = "Low" if units <= 2 else "Medium" if units <= 3 else "High"
    reason = f"{label} | EV: {ev_pct:.1%} | Risk: {risk_label}"
    return reason, ev_range


def build_recommendation(
    sport:          str,
    game:           str,
    bet:            str,
    ev_result:      EVResult,
    confidence:     ConfidenceResult,
    risk:           RiskAssessment,
    comparison:     BookComparison | None,
    ai_reasoning:   str,
    key_factors:    list[str],
    market:         str = "h2h",
    statistical_score: float = 0.5,
    ml_score:          float = 0.5,
    market_score:      float = 0.5,
    trend_score:       float = 0.5,
) -> PickRecommendation:

    units      = risk.units_allowed if risk.approved else 0
    rec        = "BET" if (units > 0 and ev_result.is_positive_ev) else "PASS"
    reason, ev_range = _unit_reason(units, ev_result.ev_pct)
    clv_potential = max(0.0, ev_result.ev_pct * 0.6)   # rough CLV estimate

    opp_score  = _opportunity_score(ev_result.ev_pct, confidence.calibrated_score, units, clv_potential)
    book_table = comparison.discord_table() if comparison else "No comparison available"
    br_examples= bankroll_examples(units, UNIT_SIZE_PCT, ev_result.american_odds)

    return PickRecommendation(
        sport              = sport,
        game               = game,
        bet                = bet,
        market             = market,
        sportsbook         = comparison.best_book if comparison else "",
        odds               = ev_result.american_odds,
        recommendation     = rec,
        confidence_pct     = round(confidence.calibrated_score * 100, 1),
        risk_score         = risk.risk_score,
        ev_pct             = ev_result.ev_pct,
        opportunity_score  = opp_score,
        units              = units,
        unit_reason        = reason,
        ev_range           = ev_range,
        bankroll_examples  = br_examples,
        book_comparison_table = book_table,
        clv_potential      = clv_potential,
        reasoning          = ai_reasoning,
        key_factors        = key_factors,
        risk_flags         = risk.red_flags,
        statistical_score  = statistical_score,
        ml_score           = ml_score,
        market_score       = market_score,
        trend_score        = trend_score,
    )


def persist_pick(
    pick: PickRecommendation,
    game_id: int,
    odds_by_book: dict | None = None,
) -> int | None:
    """
    Run the pick gate, then save to DB. Returns pick ID, or None if gate blocked it.

    BET picks that fail the gate are logged and audited but never posted.
    PASS picks bypass the gate — recording them is always correct.
    """
    from src.engines.pick_gate import check as gate_check
    from src.db.session import get_db
    from src.db.models import Pick, AuditTrail

    gate = gate_check(pick, odds_by_book=odds_by_book)

    if not gate.passed:
        # Audit the block so we can review what was rejected and why
        try:
            with get_db() as db:
                db.add(AuditTrail(
                    event_type  = "pick_blocked",
                    entity_type = "pick",
                    entity_id   = 0,
                    payload     = {
                        "sport":   pick.sport,
                        "game":    pick.game,
                        "bet":     pick.bet,
                        "ev":      pick.ev_pct,
                        "reasons": gate.reasons,
                    },
                ))
        except Exception as _audit_exc:
            logger.error("Audit trail write failed for blocked pick '%s': %s", pick.bet, _audit_exc)
        return None

    with get_db() as db:
        p = Pick(
            game_id              = game_id,
            sport                = pick.sport,
            market               = pick.market,
            selection            = pick.bet,
            best_book            = pick.sportsbook,
            american_odds_at_gen = pick.odds,
            decimal_odds_at_gen  = american_to_decimal(pick.odds) if pick.odds else 2.0,
            recommendation       = pick.recommendation,
            units                = pick.units,
            ev_pct               = pick.ev_pct,
            confidence_pct       = pick.confidence_pct,
            risk_score           = pick.risk_score,
            opportunity_score    = pick.opportunity_score,
            statistical_score    = pick.statistical_score,
            ml_score             = pick.ml_score,
            market_score         = pick.market_score,
            trend_score          = pick.trend_score,
            reasoning            = pick.reasoning,
            key_factors          = pick.key_factors,
            risk_flags           = (pick.risk_flags or []) + (gate.warnings or []),
        )
        db.add(p)
        db.flush()
        pick_id = p.id

        db.add(AuditTrail(
            event_type  = "pick_generated",
            entity_type = "pick",
            entity_id   = pick_id,
            payload     = {
                "sport": pick.sport, "game": pick.game,
                "bet": pick.bet, "units": pick.units,
                "ev": pick.ev_pct, "rec": pick.recommendation,
                "gate_warnings": gate.warnings,
            },
        ))
    return pick_id
