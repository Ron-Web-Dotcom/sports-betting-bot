"""
Parlay builder — constructs optimal parlays from ranked signals.

Strategy:
  1. Take top-ranked signals from the signal engine
  2. Filter out correlated legs (same event = SGP — handled separately)
  3. Build standard cross-game parlays (2–4 legs)
  4. Compute combined EV using fractional Kelly on the parlay
  5. Return ranked parlay candidates
"""
import logging
from dataclasses import dataclass, field
from itertools import combinations

from src.core.signal_engine import Signal
from src.core.risk_manager import parlay_ev, parlay_win_prob, size_parlay
from src.apis.odds_api import american_to_decimal
from config.settings import MIN_PARLAY_LEGS, MAX_PARLAY_LEGS, MIN_EDGE_THRESHOLD

logger = logging.getLogger(__name__)


@dataclass
class ParlayCandidate:
    legs:            list[Signal]
    combined_decimal: float
    win_probability:  float
    ev:               float
    sizing:           dict
    parlay_type:      str = "standard"

    @property
    def american_odds(self) -> int:
        dec = self.combined_decimal
        if dec >= 2.0:
            return int((dec - 1) * 100)
        return int(-100 / (dec - 1))

    @property
    def leg_summary(self) -> str:
        return " + ".join(
            f"{s.selection} ({s.event_name.split(' vs ')[0].strip()} {s.american_odds:+d})"
            for s in self.legs
        )


def _combined_decimal(legs: list[Signal]) -> float:
    result = 1.0
    for leg in legs:
        result *= american_to_decimal(leg.american_odds)
    return result


def _are_correlated(sig_a: Signal, sig_b: Signal) -> bool:
    """Same game parlays are correlated — skip for standard parlays."""
    return sig_a.event_id == sig_b.event_id


def build_standard_parlays(signals: list[Signal]) -> list[ParlayCandidate]:
    """
    Build all viable 2–4 leg parlays from a ranked signal list.
    Returns candidates sorted by EV descending.
    """
    candidates = []

    for n_legs in range(MIN_PARLAY_LEGS, MAX_PARLAY_LEGS + 1):
        for combo in combinations(signals, n_legs):
            legs = list(combo)

            # Skip if any two legs are from the same game (correlated)
            correlated = any(
                _are_correlated(a, b)
                for i, a in enumerate(legs)
                for b in legs[i + 1:]
            )
            if correlated:
                continue

            # All legs must pass minimum confidence
            if any(s.ai_confidence < 0.55 for s in legs):
                continue

            combined_dec = _combined_decimal(legs)
            leg_probs    = [s.win_probability for s in legs]
            ev           = parlay_ev(leg_probs, combined_dec)

            if ev < MIN_EDGE_THRESHOLD:
                continue

            sizing = size_parlay(leg_probs, combined_dec)
            if not sizing["approved"]:
                continue

            candidates.append(ParlayCandidate(
                legs=legs,
                combined_decimal=combined_dec,
                win_probability=parlay_win_prob(leg_probs),
                ev=ev,
                sizing=sizing,
            ))

    candidates.sort(key=lambda c: c.ev, reverse=True)
    logger.info("Built %d viable parlay candidates from %d signals", len(candidates), len(signals))
    return candidates


def build_same_game_parlay(signals: list[Signal], event_id: str) -> ParlayCandidate | None:
    """
    Build a same-game parlay from signals on the same event.
    SGPs use lower confidence threshold since legs are partially correlated.
    """
    sgp_legs = [s for s in signals if s.event_id == event_id]
    if len(sgp_legs) < 2:
        return None

    # Use top 3 legs max for SGP
    sgp_legs = sorted(sgp_legs, key=lambda s: s.composite_score, reverse=True)[:3]
    combined_dec = _combined_decimal(sgp_legs)
    leg_probs    = [s.win_probability for s in sgp_legs]

    # Apply SGP correlation discount — reduce each prob by 10%
    adjusted_probs = [max(0.01, p * 0.90) for p in leg_probs]
    ev    = parlay_ev(adjusted_probs, combined_dec)
    sizing = size_parlay(adjusted_probs, combined_dec)

    if not sizing["approved"]:
        return None

    return ParlayCandidate(
        legs=sgp_legs,
        combined_decimal=combined_dec,
        win_probability=parlay_win_prob(adjusted_probs),
        ev=ev,
        sizing=sizing,
        parlay_type="sgp",
    )


def top_parlays(candidates: list[ParlayCandidate], limit: int = 3) -> list[ParlayCandidate]:
    return candidates[:limit]
