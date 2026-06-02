"""Execution engine — places straight bets and parlays (paper or live)."""
import logging
from config.settings import PAPER_TRADING
from src.core.database import insert_position, insert_parlay
from src.apis.odds_api import american_to_decimal
from src.core.signal_engine import Signal
from src.core.parlay_builder import ParlayCandidate

logger = logging.getLogger(__name__)
_MODE = "PAPER" if PAPER_TRADING else "LIVE"


class Executor:
    def __init__(self) -> None:
        if PAPER_TRADING:
            logger.info("🟡 Paper trading mode — no real money at risk")
        else:
            logger.warning("🔴 LIVE trading mode active")

    def place_straight_bet(self, signal: Signal, sizing: dict, parlay_id: int | None = None) -> int:
        """Record a straight bet. Returns position DB id."""
        dec = american_to_decimal(signal.american_odds)
        pos = {
            "sport":          signal.sport,
            "event_id":       signal.event_id,
            "event_name":     signal.event_name,
            "market":         signal.market,
            "selection":      signal.selection,
            "book":           signal.book,
            "american_odds":  signal.american_odds,
            "decimal_odds":   dec,
            "stake":          sizing["stake"],
            "kelly_fraction": sizing["kelly_fraction"],
            "edge":           sizing["edge"],
            "ai_confidence":  signal.ai_confidence,
            "parlay_id":      parlay_id,
            "signal_data":    signal.raw_data,
        }
        pos_id = insert_position(pos)
        logger.info(
            "[%s] Straight bet #%d | %s | %s @ %+d | $%.2f stake | edge=%.1f%%",
            _MODE, pos_id, signal.event_name, signal.selection,
            signal.american_odds, sizing["stake"], sizing["edge"] * 100,
        )
        return pos_id

    def place_parlay(self, candidate: ParlayCandidate) -> tuple[int, list[int]]:
        """
        Place all legs of a parlay.
        Returns (parlay_db_id, [leg_position_ids]).
        """
        # Insert parlay record first (without leg ids)
        parlay_row = {
            "legs":           [],  # filled after legs inserted
            "combined_odds":  candidate.combined_decimal,
            "stake":          candidate.sizing["stake"],
            "potential_payout": round(candidate.sizing["stake"] * candidate.combined_decimal, 2),
            "leg_count":      len(candidate.legs),
            "parlay_type":    candidate.parlay_type,
        }
        parlay_id = insert_parlay(parlay_row)

        # Insert each leg linked to this parlay
        leg_ids = []
        for sig in candidate.legs:
            leg_sizing = {
                "stake":          candidate.sizing["stake"],  # parlay stake = single stake
                "kelly_fraction": candidate.sizing["kelly_fraction"],
                "edge":           sig.edge,
            }
            pos_id = self.place_straight_bet(sig, leg_sizing, parlay_id=parlay_id)
            leg_ids.append(pos_id)

        # Update parlay with actual leg ids
        from src.core.database import get_conn
        import json
        with get_conn() as conn:
            conn.execute(
                "UPDATE parlays SET legs=? WHERE id=?",
                (json.dumps(leg_ids), parlay_id),
            )

        logger.info(
            "[%s] Parlay #%d | %d legs | combined %+d | $%.2f stake | payout=$%.2f",
            _MODE, parlay_id, len(candidate.legs),
            candidate.american_odds, candidate.sizing["stake"],
            parlay_row["potential_payout"],
        )
        return parlay_id, leg_ids
