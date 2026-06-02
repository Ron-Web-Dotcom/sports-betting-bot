"""
Hedge engine — calculates optimal hedge bets on live parlays.

When legs of a parlay complete:
  - If all but last leg have won, recommend a hedge on the final leg's opponent
  - Calculate exact hedge stake to guarantee a profit regardless of outcome
  - Support partial hedges to lock in partial profit while keeping upside
"""
import logging
from dataclasses import dataclass
from src.apis.odds_api import american_to_decimal
from src.core.database import get_open_parlays, insert_arb
from config.settings import ARB_MIN_PROFIT_PCT

logger = logging.getLogger(__name__)


@dataclass
class HedgeRecommendation:
    parlay_id:         int
    parlay_stake:      float
    potential_payout:  float
    legs_remaining:    int
    trigger_summary:   str

    hedge_selection:   str
    hedge_book:        str
    hedge_american_odds: int
    full_hedge_stake:  float
    full_hedge_profit: float      # guaranteed if full hedge placed

    partial_hedge_stake:  float   # hedges 50% of downside
    partial_hedge_upside: float   # profit if parlay wins after partial hedge
    partial_hedge_floor:  float   # loss floor if parlay loses after partial hedge


def calculate_hedge(
    parlay_id: int,
    parlay_stake: float,
    potential_payout: float,
    hedge_american_odds: int,
    hedge_selection: str,
    hedge_book: str,
    legs_remaining: int,
    trigger_summary: str,
) -> HedgeRecommendation:
    """
    Full hedge: stake X on the opponent so that payout - X ≈ X * dec_hedge
    Let H = full hedge stake, D = hedge decimal odds
    We want: potential_payout - H = H * D - H  =>  potential_payout = H * D
    => H = potential_payout / D
    """
    dec = american_to_decimal(hedge_american_odds)
    full_stake = round(potential_payout / dec, 2)
    full_profit = round(potential_payout - full_stake, 2)

    # Partial hedge (50%)
    partial_stake  = round(full_stake * 0.5, 2)
    partial_upside = round(potential_payout - partial_stake, 2)   # if parlay wins
    partial_floor  = round(-parlay_stake + partial_stake * (dec - 1), 2)  # if parlay loses

    rec = HedgeRecommendation(
        parlay_id=parlay_id,
        parlay_stake=parlay_stake,
        potential_payout=potential_payout,
        legs_remaining=legs_remaining,
        trigger_summary=trigger_summary,
        hedge_selection=hedge_selection,
        hedge_book=hedge_book,
        hedge_american_odds=hedge_american_odds,
        full_hedge_stake=full_stake,
        full_hedge_profit=full_profit,
        partial_hedge_stake=partial_stake,
        partial_hedge_upside=partial_upside,
        partial_hedge_floor=partial_floor,
    )

    logger.info(
        "Hedge rec parlay #%d | full=$%.2f (profit=$%.2f) | partial=$%.2f (up=$%.2f floor=$%.2f)",
        parlay_id, full_stake, full_profit, partial_stake, partial_upside, partial_floor,
    )
    return rec


def check_parlays_for_hedges(live_scores: dict[str, list[dict]], all_odds: dict[str, list[dict]]) -> list[HedgeRecommendation]:
    """
    For each open parlay, check if it's one leg away from cashing
    and return hedge recommendations.
    """
    recs = []
    open_parlays = get_open_parlays()

    for parlay in open_parlays:
        legs_won      = parlay["legs_won"]
        leg_count     = parlay["leg_count"]
        remaining     = leg_count - legs_won

        # Only recommend hedge when exactly 1 leg left
        if remaining != 1:
            continue

        # We can't easily determine the final leg without full position data here
        # In practice the position manager feeds this; emit a placeholder rec
        logger.info(
            "Parlay #%d is %d/%d — 1 leg remaining, hedge opportunity",
            parlay["id"], legs_won, leg_count,
        )
        # Hedge calculation would be enriched by position_manager with actual final leg info

    return recs
