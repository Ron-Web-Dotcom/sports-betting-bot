"""Main bot orchestrator — runs the scan → analyse → bet loop."""
import logging
import time
import signal
import sys
from datetime import datetime

from config.settings import SCAN_INTERVAL_SECONDS, PAPER_TRADING
from src.core.database import init_db, get_bet_stats
from src.core.risk_manager import size_bet, get_bankroll
from src.core.ai_engine import analyse_opportunity, summarise_session
from src.core.paper_trader import PaperTrader
from src.core.opportunity_finder import gather_opportunities
from src.apis.odds_api import fetch_all_odds, american_to_decimal, get_scores
from src.alerts.discord import (
    alert_bet_placed,
    alert_session_summary,
    alert_error,
    alert_startup,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger("bot")

_running = True


def _handle_signal(signum, frame):
    global _running
    logger.info("Shutdown signal received — finishing current cycle…")
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


def run_cycle(trader: PaperTrader, session_bets: list) -> None:
    logger.info("─── Scan cycle starting %s ───", datetime.utcnow().isoformat())

    # 1. Pull live odds
    all_odds = fetch_all_odds()
    if not all_odds:
        logger.warning("No odds data returned — API key missing or rate-limited")
        return

    # 2. Build opportunities
    opportunities = gather_opportunities(all_odds)

    # 3. AI analysis
    for opp in opportunities:
        signal_data = analyse_opportunity(
            event=opp["event"],
            injuries=opp["injuries"],
            news=opp["news"],
            lines=opp["lines"],
        )

        if not signal_data or not signal_data.get("should_bet"):
            continue

        confidence = signal_data.get("confidence", 0.0)
        win_prob   = signal_data.get("win_probability", 0.0)
        selection  = signal_data.get("selection", "")

        # Find best American odds for the selection
        american_odds = _find_odds_for_selection(opp["event"]["markets"], selection)
        if not american_odds:
            logger.debug("Could not find odds for selection: %s", selection)
            continue

        decimal_odds = american_to_decimal(american_odds)

        # 4. Risk / Kelly sizing
        sizing = size_bet(win_prob, decimal_odds)
        if not sizing["approved"]:
            logger.debug("Bet rejected by risk manager: %s", sizing["reason"])
            continue

        # 5. Place (paper) bet
        bet_id = trader.place_bet(
            platform=signal_data.get("recommended_platform", "the_odds_api"),
            sport=opp["event"]["sport"],
            event_id=opp["event"]["id"],
            event_name=opp["event"]["name"],
            market=signal_data.get("market", "h2h"),
            selection=selection,
            american_odds=american_odds,
            stake=sizing["stake"],
            kelly_fraction=sizing["kelly_fraction"],
            ai_confidence=confidence,
            edge=sizing["edge"],
            raw_signal=signal_data,
        )

        session_bets.append(
            {
                "id": bet_id,
                "event": opp["event"]["name"],
                "selection": selection,
                "odds": american_odds,
                "stake": sizing["stake"],
                "edge": sizing["edge"],
            }
        )

        # 6. Discord alert
        alert_bet_placed(
            event_name=opp["event"]["name"],
            selection=selection,
            market=signal_data.get("market", "h2h"),
            american_odds=american_odds,
            stake=sizing["stake"],
            edge=sizing["edge"],
            confidence=confidence,
            platform=signal_data.get("recommended_platform", "the_odds_api"),
            reasoning=signal_data.get("reasoning", ""),
            key_factors=signal_data.get("key_factors", []),
        )

    # 7. Auto-settle completed games
    live_scores = {sport: get_scores(sport) for sport in all_odds}
    trader.auto_settle_completed(live_scores)

    logger.info("─── Cycle complete — %d bets placed this session ───", len(session_bets))


def _find_odds_for_selection(markets: dict, selection: str) -> int | None:
    for mkt_data in markets.values():
        for outcome in mkt_data.get("outcomes", []):
            if outcome.get("name", "").lower() == selection.lower():
                return outcome.get("price")
    return None


def main() -> None:
    import os
    os.makedirs("logs", exist_ok=True)

    logger.info("Sports Betting Bot starting…")
    logger.info("Mode: %s", "PAPER TRADING" if PAPER_TRADING else "LIVE TRADING")

    init_db()
    trader = PaperTrader()
    bankroll = get_bankroll()

    alert_startup(bankroll)

    session_bets: list = []
    last_summary = datetime.utcnow()

    while _running:
        try:
            run_cycle(trader, session_bets)
        except Exception as exc:
            logger.exception("Unexpected error in scan cycle: %s", exc)
            alert_error(str(exc))

        if not _running:
            break

        # Post session summary every 6 hours
        now = datetime.utcnow()
        if (now - last_summary).total_seconds() > 21600 and session_bets:
            stats = get_bet_stats()
            summary = summarise_session(session_bets, get_bankroll())
            alert_session_summary(summary, stats)
            last_summary = now

        logger.info("Sleeping %ds until next scan…", SCAN_INTERVAL_SECONDS)
        for _ in range(SCAN_INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    # Final summary on shutdown
    if session_bets:
        stats = get_bet_stats()
        summary = summarise_session(session_bets, get_bankroll())
        alert_session_summary(summary, stats)

    logger.info("Bot shut down cleanly.")


if __name__ == "__main__":
    main()
