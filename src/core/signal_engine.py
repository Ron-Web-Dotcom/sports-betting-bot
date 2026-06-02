"""Multi-factor signal engine — combines AI, line movement, injury impact."""
import logging
from dataclasses import dataclass, field
from src.apis.odds_api import american_to_decimal, implied_prob
from src.core.database import get_line_movement, get_recent_injuries, insert_signal

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    sport: str
    event_id: str
    event_name: str
    market: str
    selection: str
    book: str
    american_odds: int

    # factor scores (0–1)
    ai_confidence: float    = 0.0
    line_movement_score: float = 0.0
    injury_score: float     = 0.0
    value_score: float      = 0.0
    composite_score: float  = 0.0

    # derived
    win_probability: float  = 0.0
    edge: float             = 0.0
    signal_type: str        = "value"  # value | steam | fade | sharp | parlay_leg

    raw_data: dict = field(default_factory=dict)

    @property
    def decimal_odds(self) -> float:
        return american_to_decimal(self.american_odds)


# Weights for composite score
WEIGHTS = {
    "ai_confidence":      0.45,
    "value_score":        0.25,
    "line_movement_score":0.20,
    "injury_score":       0.10,
}


def score_line_movement(event_id: str, market: str, selection: str, current_odds: int) -> float:
    """
    Returns 0–1 score based on recent line movement direction.
    Odds shortening on a selection (books making it a bigger favourite) = bullish signal.
    """
    history = get_line_movement(event_id, market, selection, hours=6)
    if len(history) < 2:
        return 0.5  # neutral when no history

    oldest = history[0]["american_odds"]
    newest = history[-1]["american_odds"]

    # Convert to implied probability to see direction
    old_prob = implied_prob(oldest)
    new_prob = implied_prob(newest)
    delta = new_prob - old_prob  # positive = books increasing implied prob (line shortening)

    # Normalise to 0–1: delta of +0.10 = 1.0, delta of -0.10 = 0.0
    score = max(0.0, min(1.0, 0.5 + delta * 5))
    return score


def score_injuries(sport: str, event_name: str) -> float:
    """
    Returns 0–1 injury impact score.
    1.0 = favourable injuries (opponent key player out)
    0.5 = neutral
    0.0 = unfavourable (your team has key injuries)
    """
    injuries = get_recent_injuries(sport, hours=24)
    if not injuries:
        return 0.5

    out_statuses = {"out", "doubtful", "questionable"}
    teams = [t.strip() for t in event_name.replace(" vs ", " ").replace(" @ ", " ").split()]

    impact = 0
    for inj in injuries:
        if inj["status"].lower() in out_statuses:
            impact += 1  # someone is out — net positive for opposing side, can't tell which
    # Simple heuristic: more injuries = more uncertainty = slight negative
    score = max(0.0, 0.5 - min(0.5, impact * 0.05))
    return score


def score_value(win_probability: float, decimal_odds: float) -> float:
    """Edge as a 0–1 score. 5%+ edge = 1.0, 0% = 0.5, negative = 0.0."""
    from src.core.risk_manager import compute_edge
    edge = compute_edge(win_probability, decimal_odds)
    return max(0.0, min(1.0, 0.5 + edge * 10))


def build_signal(
    sport: str,
    event_id: str,
    event_name: str,
    market: str,
    selection: str,
    book: str,
    american_odds: int,
    ai_win_prob: float,
    ai_confidence: float,
    ai_signal_type: str = "value",
    extra: dict | None = None,
) -> Signal:
    decimal_odds = american_to_decimal(american_odds)
    from src.core.risk_manager import compute_edge

    lm_score  = score_line_movement(event_id, market, selection, american_odds)
    inj_score = score_injuries(sport, event_name)
    val_score = score_value(ai_win_prob, decimal_odds)
    edge      = compute_edge(ai_win_prob, decimal_odds)

    composite = (
        WEIGHTS["ai_confidence"]       * ai_confidence +
        WEIGHTS["value_score"]         * val_score +
        WEIGHTS["line_movement_score"] * lm_score +
        WEIGHTS["injury_score"]        * inj_score
    )

    sig = Signal(
        sport=sport,
        event_id=event_id,
        event_name=event_name,
        market=market,
        selection=selection,
        book=book,
        american_odds=american_odds,
        ai_confidence=ai_confidence,
        line_movement_score=lm_score,
        injury_score=inj_score,
        value_score=val_score,
        composite_score=round(composite, 4),
        win_probability=ai_win_prob,
        edge=edge,
        signal_type=ai_signal_type,
        raw_data=extra or {},
    )

    # Persist signal for dashboard + audit
    insert_signal({
        "sport":           sport,
        "event_id":        event_id,
        "event_name":      event_name,
        "market":          market,
        "selection":       selection,
        "line_value":      float(american_odds),
        "public_bet_pct":  None,
        "line_movement":   lm_score,
        "injury_impact":   inj_score,
        "ai_confidence":   ai_confidence,
        "composite_score": composite,
        "signal_type":     ai_signal_type,
        "raw_data":        extra,
    })

    return sig


def rank_signals(signals: list[Signal]) -> list[Signal]:
    return sorted(signals, key=lambda s: s.composite_score, reverse=True)
