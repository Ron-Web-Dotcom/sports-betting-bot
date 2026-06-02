"""
Engine 3 — Expected Value Engine.

Computes true probability estimates, removes the bookmaker's vig,
calculates EV and assigns unit rating.
"""
from dataclasses import dataclass
from src.core.config import UNIT_TIERS, MIN_EV_PCT


@dataclass
class EVResult:
    american_odds:   int
    decimal_odds:    float
    book_implied:    float   # raw implied prob including vig
    no_vig_prob:     float   # vig-removed (sharp) probability
    projected_prob:  float   # our model's win probability
    ev_pct:          float   # expected value as fraction
    units:           int     # 0-5 unit rating
    is_positive_ev:  bool


def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1
    return 100 / abs(american) + 1


def decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int((decimal - 1) * 100)
    return int(-100 / (decimal - 1))


def implied_prob(american: int) -> float:
    return 1 / american_to_decimal(american)


def remove_vig(odds_a: int, odds_b: int) -> tuple[float, float]:
    """
    Shin method vig removal for two-outcome market.
    Returns (no_vig_prob_a, no_vig_prob_b).
    """
    p_a = implied_prob(odds_a)
    p_b = implied_prob(odds_b)
    total = p_a + p_b
    return p_a / total, p_b / total


def compute_ev(projected_prob: float, decimal_odds: float) -> float:
    """EV = p * (odds - 1) - (1-p)."""
    return projected_prob * (decimal_odds - 1) - (1 - projected_prob)


def assign_units(ev_pct: float) -> int:
    """Map EV percentage to unit rating 0-5."""
    if ev_pct < UNIT_TIERS[1][0]:   # below minimum threshold
        return 0
    for units, (low, high) in UNIT_TIERS.items():
        if low <= ev_pct < high:
            return units
    return 5


def evaluate(
    american_odds: int,
    projected_prob: float,
    opponent_odds: int | None = None,
) -> EVResult:
    """
    Full EV evaluation for a single selection.

    Args:
        american_odds:  Odds offered by the sportsbook.
        projected_prob: Our model's win probability estimate.
        opponent_odds:  Opposing side odds (used for vig removal if provided).
    """
    dec     = american_to_decimal(american_odds)
    book_imp = implied_prob(american_odds)

    if opponent_odds:
        no_vig, _ = remove_vig(american_odds, opponent_odds)
    else:
        no_vig = book_imp

    ev_pct = compute_ev(projected_prob, dec)
    units  = assign_units(ev_pct) if ev_pct >= MIN_EV_PCT else 0

    return EVResult(
        american_odds  = american_odds,
        decimal_odds   = dec,
        book_implied   = book_imp,
        no_vig_prob    = no_vig,
        projected_prob = projected_prob,
        ev_pct         = ev_pct,
        units          = units,
        is_positive_ev = ev_pct >= MIN_EV_PCT,
    )


def bankroll_examples(units: int, unit_size_pct: float = 0.01, american_odds: int = -110) -> dict:
    """Generate stake / profit examples for standard bankroll sizes."""
    bankrolls = [100, 500, 1000, 5000]
    dec = american_to_decimal(american_odds)
    examples = {}
    for br in bankrolls:
        stake  = round(br * unit_size_pct * units, 2)
        profit = round(stake * (dec - 1), 2)
        ret    = round(stake * dec, 2)
        examples[br] = {"stake": stake, "potential_profit": profit, "potential_return": ret}
    return examples
