"""Paper trading engine — simulates bet placement and settlement."""
import logging
from datetime import datetime

from config.settings import PAPER_TRADING
from src.core.database import insert_bet, settle_bet, get_open_bets
from src.core.risk_manager import update_bankroll_after_bet
from src.apis.odds_api import american_to_decimal

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self) -> None:
        self.paper = PAPER_TRADING
        if self.paper:
            logger.info("🟡 Paper trading mode — no real money at risk")
        else:
            logger.warning("🔴 LIVE trading mode — real money at risk!")

    def place_bet(
        self,
        *,
        platform: str,
        sport: str,
        event_id: str,
        event_name: str,
        market: str,
        selection: str,
        american_odds: int,
        stake: float,
        kelly_fraction: float,
        ai_confidence: float,
        edge: float,
        raw_signal: dict,
    ) -> int:
        """Record a (paper) bet. Returns the DB row id."""
        decimal_odds = american_to_decimal(american_odds)
        mode = "PAPER" if self.paper else "LIVE"
        logger.info(
            "[%s] Placing bet — %s | %s | %s @ %+d | $%.2f stake",
            mode, platform, event_name, selection, american_odds, stake,
        )
        bet = {
            "platform":       platform,
            "sport":          sport,
            "event_id":       event_id,
            "event_name":     event_name,
            "market":         market,
            "selection":      selection,
            "odds":           decimal_odds,
            "stake":          stake,
            "kelly_fraction": kelly_fraction,
            "ai_confidence":  ai_confidence,
            "edge":           edge,
            "raw_signal":     raw_signal,
        }
        bet_id = insert_bet(bet)
        logger.info("[%s] Bet #%d recorded in DB", mode, bet_id)
        return bet_id

    def settle(self, bet_id: int, result: str, decimal_odds: float, stake: float) -> None:
        """Mark bet as won/lost and update bankroll."""
        pnl = stake * (decimal_odds - 1) if result == "win" else -stake
        settle_bet(bet_id, result, round(pnl, 2))
        update_bankroll_after_bet(stake, result, decimal_odds)
        logger.info("Bet #%d settled: %s | P&L $%.2f", bet_id, result, pnl)

    def auto_settle_completed(self, live_scores: dict[str, list[dict]]) -> None:
        """
        Attempt to auto-settle pending bets using live score data.
        live_scores: {sport_key: [score_objects from OddsAPI]}
        """
        open_bets = get_open_bets()
        for bet in open_bets:
            sport_scores = live_scores.get(bet["sport"], [])
            for score in sport_scores:
                if score.get("id") != bet["event_id"]:
                    continue
                if not score.get("completed"):
                    continue

                home_score = int(score.get("scores", [{}])[0].get("score", 0))
                away_score = int(score.get("scores", [{}])[1].get("score", 0))
                winner = score.get("home_team") if home_score > away_score else score.get("away_team")
                result = "win" if winner == bet["selection"] else "loss"
                self.settle(bet["id"], result, bet["odds"], bet["stake"])
                break
