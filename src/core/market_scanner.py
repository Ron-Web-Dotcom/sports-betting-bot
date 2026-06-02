"""
Market scanner — continuous event-driven scan loop.

Mirrors the Kalshi bot's market monitor:
  - Fast loop (60s default) for odds updates + arb scan
  - Per-event signal generation + ranking
  - Feeds signals → parlay builder → executor
"""
import logging
import time
import threading
from datetime import datetime

from config.settings import SCAN_INTERVAL_SECONDS, LIVE_SCAN_INTERVAL
from src.apis.odds_api import fetch_all_odds, get_scores
from src.apis.espn import get_injuries, get_news, fetch_all_injuries
from src.core.database import save_snapshot, upsert_injuries
from src.core.arb_detector import scan_all_events
from src.core.line_shopper import shop_all_markets
from src.core.signal_engine import build_signal, rank_signals, Signal
from src.core.ai_engine import analyse_leg
from src.core.parlay_builder import build_standard_parlays, build_same_game_parlay, top_parlays
from src.core.risk_manager import size_bet
from src.core.position_manager import PositionManager
from src.core.paper_trader import Executor

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, position_manager: PositionManager, executor: Executor) -> None:
        self.pm        = position_manager
        self.executor  = executor
        self._running  = False
        self._all_odds: dict = {}
        self._injuries: dict = {}
        self._cycle_count = 0

    # ── Main loop ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        logger.info("Market scanner started")
        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                logger.exception("Scanner cycle error: %s", exc)
            if not self._running:
                break
            sleep_time = LIVE_SCAN_INTERVAL if self._has_live_games() else SCAN_INTERVAL_SECONDS
            logger.debug("Sleeping %ds…", sleep_time)
            for _ in range(sleep_time):
                if not self._running:
                    break
                time.sleep(1)

    def stop(self) -> None:
        self._running = False

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.start, daemon=True)
        t.start()
        return t

    # ── One cycle ─────────────────────────────────────────────────────────────

    def _cycle(self) -> None:
        self._cycle_count += 1
        logger.info("── Scan #%d  %s ──", self._cycle_count, datetime.utcnow().strftime("%H:%M:%S"))

        # 1. Fetch live odds
        self._all_odds = fetch_all_odds()
        if not self._all_odds:
            logger.warning("No odds returned — API key issue or rate limit")
            return

        # 2. Save snapshots for line-movement tracking
        self._snapshot_all()

        # 3. Fetch injuries every 5 cycles (ESPN rate limit friendly)
        if self._cycle_count % 5 == 1:
            raw_injuries = fetch_all_injuries()
            for sport, inj_list in raw_injuries.items():
                upsert_injuries(inj_list)
            self._injuries = raw_injuries

        # 4. Scan for arbitrage
        arbs = scan_all_events(self._all_odds)
        if arbs:
            logger.info("⚡ %d arb opportunities found", len(arbs))
            from src.alerts.discord import alert_arb
            for arb in arbs[:3]:
                alert_arb(arb)

        # 5. Settle completed positions
        settled = self.pm.settle_completed(self._all_odds)
        if settled:
            logger.info("Settled %d positions", len(settled))

        # 6. Check for hedge opportunities
        hedge_recs = self.pm.check_hedges(self._all_odds)
        if hedge_recs:
            from src.alerts.discord import alert_hedge
            for rec in hedge_recs:
                alert_hedge(rec)

        # 7. Generate signals + build parlays
        signals = self._generate_signals()
        if signals:
            ranked = rank_signals(signals)
            logger.info("Generated %d signals (top score=%.3f)", len(ranked), ranked[0].composite_score if ranked else 0)

            # Straight bets
            for sig in ranked[:5]:
                self._maybe_place_straight(sig)

            # Parlays
            parlay_candidates = build_standard_parlays(ranked)
            for candidate in top_parlays(parlay_candidates, limit=2):
                self._maybe_place_parlay(candidate)

        logger.info("── Cycle #%d complete | portfolio: %s ──",
                    self._cycle_count, self._portfolio_line())

    # ── Signal generation ──────────────────────────────────────────────────────

    def _generate_signals(self) -> list[Signal]:
        signals = []
        for sport, events in self._all_odds.items():
            injuries = self._injuries.get(sport, [])
            news     = get_news(sport)

            for event in events[:20]:  # cap per sport to control API usage
                event_id   = event.get("id", "")
                event_name = f"{event.get('away_team','')} vs {event.get('home_team','')}"

                # Build book-keyed odds map for AI
                odds_by_book: dict = {}
                for bk in event.get("bookmakers", []):
                    odds_by_book[bk["key"]] = {
                        mkt["key"]: mkt["outcomes"] for mkt in bk.get("markets", [])
                    }

                # AI analysis
                ai = analyse_leg(
                    event={
                        "id": event_id, "name": event_name, "sport": sport,
                        "home_team": event.get("home_team",""),
                        "away_team": event.get("away_team",""),
                        "commence_time": event.get("commence_time",""),
                    },
                    injuries=injuries,
                    news=news,
                    odds_by_book=odds_by_book,
                )

                if not ai or not ai.get("should_bet"):
                    continue

                # Line shop for best odds on the AI selection
                best_lines = shop_all_markets(event)
                market     = ai.get("market", "h2h")
                selection  = ai.get("selection", "")
                mkt_lines  = best_lines.get(market, {})
                best       = mkt_lines.get(selection)

                if not best:
                    continue

                sig = build_signal(
                    sport=sport,
                    event_id=event_id,
                    event_name=event_name,
                    market=market,
                    selection=selection,
                    book=best.book,
                    american_odds=best.american_odds,
                    ai_win_prob=ai.get("win_probability", 0.5),
                    ai_confidence=ai.get("confidence", 0.5),
                    ai_signal_type=ai.get("signal_type", "value"),
                    extra=ai,
                )
                signals.append(sig)

        return signals

    # ── Execution helpers ──────────────────────────────────────────────────────

    def _maybe_place_straight(self, sig: Signal) -> None:
        from src.apis.odds_api import american_to_decimal
        sizing = size_bet(sig.win_probability, sig.decimal_odds)
        if not sizing["approved"]:
            logger.debug("Straight bet rejected: %s", sizing["reason"])
            return

        pos_id = self.executor.place_straight_bet(sig, sizing)
        from src.alerts.discord import alert_bet_placed
        alert_bet_placed(
            event_name=sig.event_name,
            selection=sig.selection,
            market=sig.market,
            american_odds=sig.american_odds,
            stake=sizing["stake"],
            edge=sizing["edge"],
            confidence=sig.ai_confidence,
            book=sig.book,
            reasoning=sig.raw_data.get("reasoning", ""),
            key_factors=sig.raw_data.get("key_factors", []),
            signal_type=sig.signal_type,
        )

    def _maybe_place_parlay(self, candidate) -> None:
        parlay_id, leg_ids = self.executor.place_parlay(candidate)
        from src.alerts.discord import alert_parlay_placed
        alert_parlay_placed(candidate, parlay_id)

    # ── Utilities ──────────────────────────────────────────────────────────────

    def _snapshot_all(self) -> None:
        for sport, events in self._all_odds.items():
            for event in events:
                eid = event.get("id", "")
                for bk in event.get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        for outcome in mkt.get("outcomes", []):
                            save_snapshot(
                                book=bk["key"],
                                sport=sport,
                                event_id=eid,
                                market=mkt["key"],
                                selection=outcome["name"],
                                american_odds=int(outcome.get("price", -110)),
                            )

    def _has_live_games(self) -> bool:
        for sport, events in self._all_odds.items():
            for event in events:
                if event.get("in_play") or event.get("scores"):
                    return True
        return False

    def _portfolio_line(self) -> str:
        summary = self.pm.portfolio_summary()
        return (
            f"bankroll=${summary['bankroll']:.2f} "
            f"open={summary['open_positions']}pos+{summary['open_parlays']}parlays "
            f"exposure={summary['exposure_pct']:.1%}"
        )
