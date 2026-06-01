"""Kelly Criterion sizing + risk management guardrails."""
import logging
from config.settings import (
    BANKROLL,
    MAX_KELLY_FRACTION,
    MAX_BET_PCT,
    MIN_EDGE_THRESHOLD,
    MIN_CONFIDENCE,
)
from src.core.database import latest_bankroll, record_bankroll

logger = logging.getLogger(__name__)


def kelly_fraction(win_prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction: (bp - q) / b  where b = decimal_odds - 1."""
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    q = 1 - win_prob
    return (b * win_prob - q) / b


def fractional_kelly(win_prob: float, decimal_odds: float, fraction: float = MAX_KELLY_FRACTION) -> float:
    """Fractional Kelly — cap at MAX_KELLY_FRACTION to reduce variance."""
    fk = kelly_fraction(win_prob, decimal_odds)
    return max(0.0, fk * fraction)


def compute_edge(win_prob: float, decimal_odds: float) -> float:
    """Expected value edge = win_prob * (decimal_odds - 1) - lose_prob."""
    return win_prob * (decimal_odds - 1) - (1 - win_prob)


def get_bankroll() -> float:
    stored = latest_bankroll()
    if stored is not None:
        return stored
    record_bankroll(BANKROLL, "initial")
    return BANKROLL


def size_bet(win_prob: float, decimal_odds: float) -> dict:
    """
    Returns sizing decision:
      {
        "approved": bool,
        "stake": float,
        "kelly_fraction": float,
        "edge": float,
        "reason": str,
      }
    """
    edge = compute_edge(win_prob, decimal_odds)
    fk   = fractional_kelly(win_prob, decimal_odds)
    bankroll = get_bankroll()

    if win_prob < MIN_CONFIDENCE:
        return _reject(f"AI confidence {win_prob:.1%} < minimum {MIN_CONFIDENCE:.1%}", edge, fk)

    if edge < MIN_EDGE_THRESHOLD:
        return _reject(f"Edge {edge:.1%} < minimum {MIN_EDGE_THRESHOLD:.1%}", edge, fk)

    if fk <= 0:
        return _reject("Kelly fraction non-positive (negative EV)", edge, fk)

    raw_stake = bankroll * fk
    max_stake = bankroll * MAX_BET_PCT
    stake = min(raw_stake, max_stake)
    stake = round(stake, 2)

    logger.info(
        "Bet approved — edge=%.1f%%, kelly=%.1f%%, stake=$%.2f (bankroll=$%.2f)",
        edge * 100, fk * 100, stake, bankroll,
    )
    return {
        "approved": True,
        "stake": stake,
        "kelly_fraction": fk,
        "edge": edge,
        "reason": f"edge={edge:.1%}, kelly={fk:.1%}, stake=${stake:.2f}",
    }


def _reject(reason: str, edge: float, fk: float) -> dict:
    logger.debug("Bet rejected: %s", reason)
    return {"approved": False, "stake": 0.0, "kelly_fraction": fk, "edge": edge, "reason": reason}


def update_bankroll_after_bet(stake: float, result: str, odds: float) -> float:
    """Update and persist bankroll after settlement. result = 'win' | 'loss'."""
    bankroll = get_bankroll()
    if result == "win":
        pnl = stake * (odds - 1)
    else:
        pnl = -stake
    new_balance = round(bankroll + pnl, 2)
    record_bankroll(new_balance, f"{result}: stake={stake}, odds={odds}")
    logger.info("Bankroll updated: $%.2f → $%.2f (%+.2f)", bankroll, new_balance, pnl)
    return new_balance
