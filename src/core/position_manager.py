"""
Position manager — tracks all open positions and parlays, drives settlement,
hedge recommendations, and portfolio-level reporting.

Mirrors Kalshi bot's order/position tracker.
"""
import logging
from datetime import datetime
from src.core.database import (
    get_open_positions, get_open_parlays,
    settle_position, settle_parlay, update_parlay_leg_won,
    get_position_stats, latest_bankroll,
)
from src.core.risk_manager import update_bankroll_after_result
from src.core.hedge_engine import calculate_hedge, HedgeRecommendation
from src.apis.odds_api import get_scores, american_to_decimal

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self) -> None:
        self._hedge_recs: list[HedgeRecommendation] = []

    # ── Portfolio snapshot ─────────────────────────────────────────────────────

    def portfolio_summary(self) -> dict:
        positions = get_open_positions()
        parlays   = get_open_parlays()
        bankroll  = latest_bankroll() or 0.0

        total_staked  = sum(p["stake"] for p in positions)
        parlay_staked = sum(p["stake"] for p in parlays)
        exposure_pct  = (total_staked + parlay_staked) / bankroll if bankroll else 0.0

        return {
            "bankroll":          bankroll,
            "open_positions":    len(positions),
            "open_parlays":      len(parlays),
            "total_staked":      round(total_staked + parlay_staked, 2),
            "exposure_pct":      round(exposure_pct, 4),
            "positions":         positions,
            "parlays":           parlays,
            "pending_hedges":    len(self._hedge_recs),
            "stats":             get_position_stats(),
        }

    # ── Settlement ─────────────────────────────────────────────────────────────

    def settle_completed(self, all_odds: dict[str, list[dict]]) -> list[dict]:
        """
        Check all open positions against live score data.
        Returns list of settlement records.
        """
        settled = []
        # Build score lookup: event_id -> score_obj
        score_map: dict[str, dict] = {}
        for sport, events in all_odds.items():
            scores = get_scores(sport, days_from=1)
            for s in scores:
                score_map[s.get("id", "")] = s

        positions = get_open_positions()
        for pos in positions:
            score = score_map.get(pos["event_id"])
            if not score or not score.get("completed"):
                continue

            result = self._determine_result(pos, score)
            if result is None:
                continue

            pnl = pos["stake"] * (pos["decimal_odds"] - 1) if result == "win" else -pos["stake"]
            settle_position(pos["id"], result, round(pnl, 2))
            update_bankroll_after_result(pos["stake"], result, pos["decimal_odds"])

            # Update parlay if this leg belongs to one
            if pos.get("parlay_id"):
                if result == "win":
                    update_parlay_leg_won(pos["parlay_id"])
                else:
                    # Parlay lost — settle entire parlay as loss
                    settle_parlay(pos["parlay_id"], "loss", -self._parlay_stake(pos["parlay_id"]))

            settled.append({
                "position_id": pos["id"],
                "event_name":  pos["event_name"],
                "selection":   pos["selection"],
                "result":      result,
                "pnl":         round(pnl, 2),
                "parlay_id":   pos.get("parlay_id"),
            })
            logger.info("Settled position #%d: %s %s P&L $%.2f", pos["id"], pos["selection"], result, pnl)

        return settled

    def _determine_result(self, pos: dict, score: dict) -> str | None:
        scores_data = score.get("scores") or []
        if len(scores_data) < 2:
            return None

        try:
            home_score = float(scores_data[0].get("score", 0))
            away_score = float(scores_data[1].get("score", 0))
        except (ValueError, TypeError):
            return None

        home_team = score.get("home_team", "")
        away_team = score.get("away_team", "")
        selection = pos["selection"]

        if pos["market"] == "h2h":
            winner = home_team if home_score > away_score else away_team
            if home_score == away_score:
                return None  # draw / push
            return "win" if selection.lower() in winner.lower() else "loss"

        # spreads / totals — simplified
        return None

    def _parlay_stake(self, parlay_id: int) -> float:
        parlays = get_open_parlays()
        for p in parlays:
            if p["id"] == parlay_id:
                return p["stake"]
        return 0.0

    # ── Hedge detection ────────────────────────────────────────────────────────

    def check_hedges(self, all_odds: dict[str, list[dict]]) -> list[HedgeRecommendation]:
        recs = []
        parlays = get_open_parlays()
        positions = {p["id"]: p for p in get_open_positions()}

        # Build best-odds lookup from live odds
        best_odds_map: dict[str, dict[str, int]] = {}  # event_id -> selection -> best_odds
        for sport, events in all_odds.items():
            for event in events:
                eid = event.get("id", "")
                best_odds_map.setdefault(eid, {})
                for bk in event.get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        if mkt["key"] != "h2h":
                            continue
                        for outcome in mkt.get("outcomes", []):
                            sel = outcome["name"]
                            price = int(outcome.get("price", -110))
                            existing = best_odds_map[eid].get(sel, -99999)
                            if american_to_decimal(price) > american_to_decimal(existing):
                                best_odds_map[eid][sel] = price

        for parlay in parlays:
            legs_won  = parlay["legs_won"]
            leg_count = parlay["leg_count"]
            if leg_count - legs_won != 1:
                continue

            # Find the remaining open leg
            leg_ids = parlay["legs"]
            remaining_legs = [positions[lid] for lid in leg_ids if lid in positions and positions[lid]["status"] == "open"]
            if not remaining_legs:
                continue

            final_leg = remaining_legs[0]
            event_id  = final_leg["event_id"]
            selection = final_leg["selection"]

            # Find the opposing selection's best odds
            event_odds = best_odds_map.get(event_id, {})
            opposing = {k: v for k, v in event_odds.items() if k.lower() != selection.lower()}
            if not opposing:
                continue

            opp_selection, opp_odds = max(opposing.items(), key=lambda x: american_to_decimal(x[1]))

            rec = calculate_hedge(
                parlay_id=parlay["id"],
                parlay_stake=parlay["stake"],
                potential_payout=parlay["potential_payout"],
                hedge_american_odds=opp_odds,
                hedge_selection=opp_selection,
                hedge_book="best_available",
                legs_remaining=1,
                trigger_summary=f"{legs_won}/{leg_count} legs won",
            )
            recs.append(rec)

        self._hedge_recs = recs
        return recs

    def get_pending_hedges(self) -> list[HedgeRecommendation]:
        return self._hedge_recs
