"""
Engine 4 — Confidence Engine.

Produces a calibrated confidence score (0.0-1.0) by combining:
  - AI win probability
  - Model consensus agreement
  - Line movement direction (sharp money indicator)
  - News/injury impact
  - Historical calibration correction

Continuously recalibrated via the self-improvement engine.
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Signal weights — must sum to 1.0 so perfect inputs yield raw_score of 1.0.
# Calibration is an additive correction applied separately (not a weight).
WEIGHTS = {
    "ai_prob":        0.40,
    "model_consensus":0.25,
    "line_movement":  0.20,
    "news_impact":    0.15,
    "calibration":    0.10,   # kept for backward compat — not used in raw sum
}


@dataclass
class ConfidenceResult:
    raw_score:        float   # 0-1 before calibration
    calibrated_score: float   # 0-1 after historical correction
    ai_prob:          float
    model_consensus:  float   # fraction of models agreeing (0-1)
    line_movement:    float   # 0-1 (1 = sharp money on our side)
    news_impact:      float   # 0-1 (1 = news strongly supports bet)
    calibration_adj:  float   # correction from historical performance
    confidence_bucket:str     # "60-65", "65-70" etc.


def _bucket(score: float) -> str:
    pct = score * 100
    low = int(pct // 5) * 5
    return f"{low}-{low+5}"


def get_calibration_adjustment(sport: str, market: str, bucket: str) -> float:
    """
    Look up historical calibration correction for this sport/market/bucket.
    Returns adjustment as additive correction to confidence (can be negative).
    """
    try:
        from src.db.session import get_db
        from src.db.models import ConfidenceCalibration
        with get_db() as db:
            rec = db.query(ConfidenceCalibration).filter_by(
                sport=sport, market=market, confidence_bucket=bucket,
            ).first()
            if rec and rec.actual_win_rate is not None and rec.sample_size >= 20:
                return rec.actual_win_rate - rec.predicted_win_rate
    except Exception as e:
        logger.warning("Calibration lookup failed: %s", e)
    return 0.0


def compute_confidence(
    ai_win_prob:       float,
    model_consensus:   float,
    line_movement_score: float,
    news_impact_score: float,
    sport:             str = "",
    market:            str = "",
) -> ConfidenceResult:
    """
    Combine all signals into a calibrated confidence score.

    All inputs are 0.0-1.0.
    ai_win_prob       = Claude's estimated win probability
    model_consensus   = fraction of sub-models agreeing with the bet
    line_movement_score = 0=against, 0.5=neutral, 1=sharp money aligned
    news_impact_score  = 0=bad news, 0.5=neutral, 1=good news
    """
    raw = (
        WEIGHTS["ai_prob"]         * ai_win_prob +
        WEIGHTS["model_consensus"] * model_consensus +
        WEIGHTS["line_movement"]   * line_movement_score +
        WEIGHTS["news_impact"]     * news_impact_score
    )
    raw = max(0.0, min(1.0, raw))

    bucket = _bucket(raw)
    # Calibration adj is a direct additive correction (e.g. +0.03 if we outperform
    # predicted win rate in this bucket). Do NOT multiply by the calibration weight —
    # that dampened a 7% correction down to 0.7%, making calibration useless.
    adj    = get_calibration_adjustment(sport, market, bucket)
    calibrated = max(0.0, min(1.0, raw + adj))

    return ConfidenceResult(
        raw_score        = round(raw, 4),
        calibrated_score = round(calibrated, 4),
        ai_prob          = ai_win_prob,
        model_consensus  = model_consensus,
        line_movement    = line_movement_score,
        news_impact      = news_impact_score,
        calibration_adj  = adj,
        confidence_bucket= bucket,
    )


def update_calibration(sport: str, market: str, bucket: str, won: bool) -> None:
    """Called on settlement to update the calibration table."""
    try:
        from src.db.session import get_db
        from src.db.models import ConfidenceCalibration
        with get_db() as db:
            rec = db.query(ConfidenceCalibration).filter_by(
                sport=sport, market=market, confidence_bucket=bucket,
            ).first()
            if not rec:
                rec = ConfidenceCalibration(
                    sport=sport, market=market, confidence_bucket=bucket,
                    predicted_win_rate=float(bucket.split("-")[0]) / 100,
                    actual_win_rate=0.0, sample_size=0,
                )
                db.add(rec)
            n = rec.sample_size
            rec.actual_win_rate = (rec.actual_win_rate * n + (1.0 if won else 0.0)) / (n + 1)
            rec.sample_size     = n + 1
            rec.calibration_error = abs(rec.predicted_win_rate - rec.actual_win_rate)
    except Exception as e:
        logger.error("Calibration update failed: %s", e)
