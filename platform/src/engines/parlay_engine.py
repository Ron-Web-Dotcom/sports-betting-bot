"""
Engine 6 — Parlay Correlation Engine.

Analyses leg combinations for positive/negative correlation.
Adjusts combined EV and confidence accordingly.
Builds standard parlays, SGPs, and round robins.
"""
import logging
from dataclasses import dataclass, field
from itertools import combinations
from src.engines.ev_engine import american_to_decimal, compute_ev

logger = logging.getLogger(__name__)

# Correlation coefficients: (market_a, market_b) → estimated rho
# Positive = legs move together (reduces diversification)
# Negative = legs hedge each other
CORRELATION_MAP = {
    ("h2h",    "spreads"):  0.85,   # ML and spread on same team: very correlated
    ("h2h",    "totals"):   0.25,   # some correlation (high-scoring teams win)
    ("spreads","totals"):   0.15,
    ("h2h",    "player_prop"): 0.40, # player does well when team wins
    ("player_prop","player_prop"): 0.30,  # same team props correlated
}


@dataclass
class ParlayLeg:
    event_id:      str
    event_name:    str
    sport:         str
    market:        str
    selection:     str
    book:          str
    american_odds: int
    win_probability: float
    ev_pct:        float
    confidence:    float


@dataclass
class ParlayResult:
    legs:             list[ParlayLeg]
    parlay_type:      str          # standard | sgp | round_robin
    combined_decimal: float
    raw_win_prob:     float        # product of leg probs (assumes independence)
    adjusted_win_prob:float        # after correlation adjustment
    raw_ev:           float
    adjusted_ev:      float
    correlation_risk: str          # low | medium | high
    units:            int
    sizing_notes:     str
    leg_summary:      str = field(init=False)

    def __post_init__(self):
        self.leg_summary = " + ".join(
            f"{l.selection} ({l.american_odds:+d})" for l in self.legs
        )

    @property
    def american_odds(self) -> int:
        dec = self.combined_decimal
        if dec <= 1.0:
            return -10000
        return int((dec-1)*100) if dec >= 2 else int(-100/(dec-1))


def _correlation_penalty(legs: list[ParlayLeg]) -> float:
    """
    Estimate average pairwise correlation between all legs.
    Returns 0.0-1.0 (higher = more correlated = riskier parlay).
    """
    if len(legs) < 2:
        return 0.0
    total_rho = 0.0
    pairs = 0
    for a, b in combinations(legs, 2):
        same_game = a.event_id == b.event_id
        rho = CORRELATION_MAP.get((a.market, b.market), CORRELATION_MAP.get((b.market, a.market), 0.0))
        if same_game:
            rho = min(1.0, rho + 0.30)  # SGP penalty
        total_rho += rho
        pairs += 1
    return total_rho / pairs if pairs else 0.0


def _adjusted_win_prob(legs: list[ParlayLeg]) -> float:
    """
    Adjust combined win probability for correlation.
    Positive correlation reduces true variance — but also reduces true EV when correlated legs all fail.
    Conservative approach: multiply raw prob by (1 - rho * 0.15).
    """
    raw = 1.0
    for leg in legs:
        raw *= leg.win_probability
    rho = _correlation_penalty(legs)
    # Positive correlation: slight upward adjustment if legs move together (SGP upside)
    # but we use a conservative 0.95 discount
    return min(1.0, raw * (1.0 - rho * 0.10))


def _correlation_risk_label(rho: float) -> str:
    if rho < 0.20:  return "low"
    if rho < 0.45:  return "medium"
    return "high"


def build_parlay(legs: list[ParlayLeg], parlay_type: str = "standard") -> ParlayResult:
    combined_dec   = 1.0
    for leg in legs:
        combined_dec *= american_to_decimal(leg.american_odds)

    raw_prob  = 1.0
    for leg in legs:
        raw_prob *= leg.win_probability

    adj_prob  = _adjusted_win_prob(legs)
    rho       = _correlation_penalty(legs)
    raw_ev    = compute_ev(raw_prob,  combined_dec)
    adj_ev    = compute_ev(adj_prob,  combined_dec)

    # Units: use adjusted EV (more conservative for parlays)
    from src.engines.ev_engine import assign_units
    units = assign_units(adj_ev)

    return ParlayResult(
        legs             = legs,
        parlay_type      = parlay_type,
        combined_decimal = combined_dec,
        raw_win_prob     = round(raw_prob, 4),
        adjusted_win_prob= round(adj_prob, 4),
        raw_ev           = round(raw_ev,   4),
        adjusted_ev      = round(adj_ev,   4),
        correlation_risk = _correlation_risk_label(rho),
        units            = units,
        sizing_notes     = f"Correlation={rho:.0%} ({_correlation_risk_label(rho)} risk)",
    )


def find_best_parlays(
    all_legs: list[ParlayLeg],
    min_legs:  int = 2,
    max_legs:  int = 4,
    min_ev:    float = 0.02,
    top_n:     int = 5,
) -> list[ParlayResult]:
    candidates: list[ParlayResult] = []
    eligible = [l for l in all_legs if l.ev_pct > 0 and l.confidence > 0.55]

    for n in range(min_legs, max_legs + 1):
        for combo in combinations(eligible, n):
            # Skip if any two legs are from the same game (for standard parlays)
            same_game_count = sum(
                1 for a, b in combinations(combo, 2) if a.event_id == b.event_id
            )
            if same_game_count > 0 and not any(l.market == "player_prop" for l in combo):
                continue  # Skip pure same-game combos (handled by SGP builder)
            result = build_parlay(list(combo))
            if result.adjusted_ev >= min_ev:
                candidates.append(result)

    candidates.sort(key=lambda x: x.adjusted_ev, reverse=True)
    return candidates[:top_n]


def build_round_robin(legs: list[ParlayLeg], size: int = 2) -> list[ParlayResult]:
    """Build all possible size-leg parlays from the leg list (round robin)."""
    return [build_parlay(list(c), "round_robin") for c in combinations(legs, size)]
