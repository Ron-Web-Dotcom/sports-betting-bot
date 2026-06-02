"""Portfolio-level Kelly sizing + risk guardrails."""
import logging
from config.settings import (
    BANKROLL, MAX_KELLY_FRACTION, MAX_BET_PCT, MAX_PARLAY_PCT,
    MAX_PORTFOLIO_EXPOSURE, MIN_EDGE_THRESHOLD, MIN_CONFIDENCE,
)
from src.core.database import latest_bankroll, record_bankroll, get_open_positions

logger = logging.getLogger(__name__)


# ── Kelly math ────────────────────────────────────────────────────────────────

def kelly_fraction(win_prob: float, decimal_odds: float) -> float:
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    return (b * win_prob - (1 - win_prob)) / b


def fractional_kelly(win_prob: float, decimal_odds: float, fraction: float = MAX_KELLY_FRACTION) -> float:
    return max(0.0, kelly_fraction(win_prob, decimal_odds) * fraction)


def compute_edge(win_prob: float, decimal_odds: float) -> float:
    return win_prob * (decimal_odds - 1) - (1 - win_prob)


def parlay_win_prob(leg_probs: list[float]) -> float:
    result = 1.0
    for p in leg_probs:
        result *= p
    return result


def parlay_ev(leg_probs: list[float], combined_decimal: float) -> float:
    wp = parlay_win_prob(leg_probs)
    return compute_edge(wp, combined_decimal)


# ── Bankroll ──────────────────────────────────────────────────────────────────

def get_bankroll() -> float:
    stored = latest_bankroll()
    if stored is not None:
        return stored
    record_bankroll(BANKROLL, "initial")
    return BANKROLL


def current_portfolio_exposure() -> float:
    """Sum of all open position stakes as fraction of bankroll."""
    bankroll = get_bankroll()
    if bankroll <= 0:
        return 1.0
    open_positions = get_open_positions()
    total_at_risk = sum(p["stake"] for p in open_positions)
    return total_at_risk / bankroll


# ── Straight bet sizing ───────────────────────────────────────────────────────

def size_bet(win_prob: float, decimal_odds: float, is_parlay_leg: bool = False) -> dict:
    edge = compute_edge(win_prob, decimal_odds)
    fk   = fractional_kelly(win_prob, decimal_odds)
    bankroll = get_bankroll()
    exposure = current_portfolio_exposure()
    max_pct  = MAX_PARLAY_PCT if is_parlay_leg else MAX_BET_PCT

    if win_prob < MIN_CONFIDENCE:
        return _reject(f"confidence {win_prob:.1%} < {MIN_CONFIDENCE:.1%}", edge, fk)
    if edge < MIN_EDGE_THRESHOLD:
        return _reject(f"edge {edge:.1%} < {MIN_EDGE_THRESHOLD:.1%}", edge, fk)
    if fk <= 0:
        return _reject("non-positive Kelly (negative EV)", edge, fk)
    if exposure >= MAX_PORTFOLIO_EXPOSURE:
        return _reject(f"portfolio exposure {exposure:.1%} >= {MAX_PORTFOLIO_EXPOSURE:.1%}", edge, fk)

    raw_stake = bankroll * fk
    max_stake = bankroll * max_pct
    # Also cap so we don't exceed portfolio limit
    remaining_capacity = bankroll * (MAX_PORTFOLIO_EXPOSURE - exposure)
    stake = round(min(raw_stake, max_stake, remaining_capacity), 2)
    stake = max(stake, 0.01)

    return {
        "approved": True,
        "stake": stake,
        "kelly_fraction": fk,
        "edge": edge,
        "reason": f"edge={edge:.1%} kelly={fk:.1%} stake=${stake:.2f}",
    }


# ── Parlay sizing ─────────────────────────────────────────────────────────────

def size_parlay(leg_probs: list[float], combined_decimal: float) -> dict:
    ev = parlay_ev(leg_probs, combined_decimal)
    wp = parlay_win_prob(leg_probs)
    fk = fractional_kelly(wp, combined_decimal)
    bankroll = get_bankroll()
    exposure = current_portfolio_exposure()

    if ev < MIN_EDGE_THRESHOLD:
        return _reject(f"parlay EV {ev:.1%} < {MIN_EDGE_THRESHOLD:.1%}", ev, fk)
    if exposure >= MAX_PORTFOLIO_EXPOSURE:
        return _reject(f"portfolio exposure {exposure:.1%} >= {MAX_PORTFOLIO_EXPOSURE:.1%}", ev, fk)

    raw_stake = bankroll * fk
    max_stake = bankroll * MAX_PARLAY_PCT
    remaining  = bankroll * (MAX_PORTFOLIO_EXPOSURE - exposure)
    stake = round(min(raw_stake, max_stake, remaining), 2)
    stake = max(stake, 0.01)

    return {
        "approved": True,
        "stake": stake,
        "kelly_fraction": fk,
        "edge": ev,
        "win_probability": wp,
        "reason": f"parlay_ev={ev:.1%} win_prob={wp:.1%} stake=${stake:.2f}",
    }


# ── Settlement ────────────────────────────────────────────────────────────────

def update_bankroll_after_result(stake: float, result: str, decimal_odds: float) -> float:
    bankroll = get_bankroll()
    pnl = stake * (decimal_odds - 1) if result == "win" else -stake
    new_balance = round(bankroll + pnl, 2)
    record_bankroll(new_balance, f"{result}: stake={stake:.2f} odds={decimal_odds:.3f}")
    logger.info("Bankroll $%.2f → $%.2f (%+.2f)", bankroll, new_balance, pnl)
    return new_balance


def _reject(reason: str, edge: float, fk: float) -> dict:
    return {"approved": False, "stake": 0.0, "kelly_fraction": fk, "edge": edge, "reason": reason}
