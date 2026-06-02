"""
Sports Betting Bot — main entry point.

Usage:
  python bot.py            # run bot + live dashboard
  python bot.py --no-dash  # headless (good for systemd)
  python bot.py --dash     # dashboard only (read-only)
"""
import sys
import signal
import logging
import argparse
import threading
from pathlib import Path

# ── Logging setup (must happen before any imports that use logging) ────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log"),
    ],
)
logger = logging.getLogger("bot")

from config.settings import PAPER_TRADING
from src.core.database import init_db
from src.core.risk_manager import get_bankroll
from src.core.position_manager import PositionManager
from src.core.paper_trader import Executor
from src.core.market_scanner import MarketScanner
from src.alerts.discord import alert_startup, alert_error, alert_session_summary
from src.core.database import get_position_stats
from src.core.ai_engine import write_session_summary
from src.core.database import get_open_positions, get_open_parlays


def parse_args():
    p = argparse.ArgumentParser(description="Sports Betting Bot")
    p.add_argument("--no-dash",  action="store_true", help="Disable live dashboard (headless mode)")
    p.add_argument("--dash",     action="store_true", help="Dashboard only, no trading")
    p.add_argument("--paper",    action="store_true", help="Force paper trading mode")
    return p.parse_args()


def main():
    args = parse_args()

    logger.info("━━ Sports Betting Bot ━━")
    logger.info("Mode: %s", "PAPER TRADING" if PAPER_TRADING else "⚠ LIVE TRADING")

    # Init DB
    init_db()
    bankroll = get_bankroll()
    logger.info("Bankroll: $%.2f", bankroll)

    # Dashboard-only mode
    if args.dash:
        from src.dashboard.cli import run_live
        run_live()
        return

    # Components
    pm       = PositionManager()
    executor = Executor()
    scanner  = MarketScanner(pm, executor)

    # Graceful shutdown
    _stop = threading.Event()

    def _shutdown(signum, frame):
        logger.info("Shutdown signal received…")
        _stop.set()
        scanner.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # Startup Discord ping
    alert_startup(bankroll)

    # Start scanner in background thread
    scan_thread = scanner.start_background()

    # Start dashboard in main thread (unless headless)
    if not args.no_dash:
        try:
            from src.dashboard.cli import run_live
            dash_thread = threading.Thread(target=run_live, kwargs={"refresh_seconds": 5}, daemon=True)
            dash_thread.start()
        except Exception as exc:
            logger.warning("Dashboard failed to start: %s", exc)

    # Wait for shutdown signal
    try:
        _stop.wait()
    except KeyboardInterrupt:
        pass

    scanner.stop()
    scan_thread.join(timeout=10)

    # Final summary
    try:
        stats     = get_position_stats()
        positions = get_open_positions()
        parlays   = get_open_parlays()
        summary   = write_session_summary(positions, parlays, get_bankroll())
        alert_session_summary(summary, stats)
    except Exception as exc:
        logger.error("Final summary error: %s", exc)

    logger.info("Bot shut down cleanly.")


if __name__ == "__main__":
    main()
