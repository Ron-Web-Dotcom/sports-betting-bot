"""
Sports Bot Runner — replaces Celery entirely.

Single process, no broker, no worker. Runs all tasks directly on a
schedule using a simple time-tracking loop. Same as how simple bots work.
"""
import logging
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Ensure log directory exists before FileHandler tries to open it
Path("/var/log/sports-bot").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/sports-bot/runner.log"),
    ],
)
# Silence noisy libs
for _log in ("httpx", "httpcore", "openai._base_client", "tenacity"):
    logging.getLogger(_log).setLevel(logging.WARNING)

logger = logging.getLogger("runner")

# ── Task imports ───────────────────────────────────────────────────────────────

def _import_tasks():
    from src.workers.odds_worker            import scan_and_save_odds, scan_player_props, refresh_active_sports
    from src.workers.news_worker            import fetch_and_save_news
    from src.workers.picks_worker           import generate_picks, generate_parlays, scan_todays_games, generate_hardrock_day_entry, generate_hardrock_night_entry
    from src.workers.alert_worker           import send_pregame_alerts
    from src.workers.prediction_market_worker import (
        scan_prediction_markets,
        generate_prediction_market_day_entry,
        generate_prediction_market_night_entry,
    )
    from src.workers.settlement_worker import settle_completed_picks, record_closing_lines
    from src.workers.slip_tracker import track_slips
    from src.workers.analytics_worker  import (
        enter_sleep_mode, wake_up_brief, send_daily_summary,
        send_weekly_summary, send_weekly_fresh_start, run_self_improvement,
        snapshot_portfolio, send_monthly_summary, yesterday_recap,
        cleanup_old_snapshots, flush_memory,
    )
    return {
        "scan_prediction_markets":                  scan_prediction_markets,
        "generate_prediction_market_day_entry":     generate_prediction_market_day_entry,
        "generate_prediction_market_night_entry":   generate_prediction_market_night_entry,
        "scan_and_save_odds":         scan_and_save_odds,
        "scan_player_props":      scan_player_props,
        "refresh_active_sports":  refresh_active_sports,
        "fetch_and_save_news":    fetch_and_save_news,
        "generate_picks":               generate_picks,
        "generate_parlays":             generate_parlays,
        "scan_todays_games":            scan_todays_games,
        "generate_hardrock_day_entry":  generate_hardrock_day_entry,
        "generate_hardrock_night_entry":generate_hardrock_night_entry,
        "send_pregame_alerts":    send_pregame_alerts,
        "settle_completed_picks": settle_completed_picks,
        "record_closing_lines":   record_closing_lines,
        "track_slips":            track_slips,
        "enter_sleep_mode":       enter_sleep_mode,
        "wake_up_brief":          wake_up_brief,
        "send_daily_summary":     send_daily_summary,
        "send_weekly_summary":    send_weekly_summary,
        "send_weekly_fresh_start":send_weekly_fresh_start,
        "run_self_improvement":   run_self_improvement,
        "snapshot_portfolio":     snapshot_portfolio,
        "send_monthly_summary":   send_monthly_summary,
        "yesterday_recap":        yesterday_recap,
        "cleanup_old_snapshots":  cleanup_old_snapshots,
        "flush_memory":           flush_memory,
    }


# ── Schedule definition ────────────────────────────────────────────────────────
# interval tasks: run every N seconds
# cron tasks:     run at specific (hour, minute) ET — optionally day_of_week / day_of_month

INTERVAL_TASKS = [
    # (interval_seconds, task_name)
    (180,  "track_slips"),              # 3 min — slip lifecycle alerts (soon/live/result/W-L)
    (180,  "scan_prediction_markets"), # 3 min — catches in-game price swings on Kalshi/Poly
    (300,  "send_pregame_alerts"),     # 5 min
    (600,  "scan_and_save_odds"),      # 10 min — odds don't move every 5 min
    (1200, "scan_player_props"),       # 20 min — props are stable
    (1200, "generate_picks"),          # 20 min — unified game + prop picks
    (1800, "fetch_and_save_news"),     # 30 min
    (1800, "settle_completed_picks"),  # 30 min
    (3600, "record_closing_lines"),    # 60 min
]

CRON_TASKS = [
    # (hour, minute, task_name, day_of_week=None, day_of_month=None)
    # day_of_week: 0=Monday … 6=Sunday  (Python weekday())
    (0,  5,  "snapshot_portfolio",      None, None),
    (0,  1,  "send_monthly_summary",    None, 1),     # 1st of month
    (2,  0,  "run_self_improvement",    None, None),
    (2,  55, "cleanup_old_snapshots",   None, None),
    (2,  58, "flush_memory",            None, None),
    (3,  0,  "enter_sleep_mode",        None, None),
    (5,  0,  "wake_up_brief",            None, None),
    (5,  30, "refresh_active_sports",   None, None),  # refresh after wake, before first scan
    (6,  0,  "yesterday_recap",         None, None),
    (8,  0,  "scan_todays_games",               None, None),  # Sofascore full scan — split day/night, cache
    (9,  0,  "generate_parlays",               None, None),
    (10, 30, "generate_hardrock_day_entry",               None, None),  # HardRock day entry
    (10, 35, "generate_prediction_market_day_entry",     None, None),  # Kalshi/Poly day entry (5 min after)
    (14, 0,  "scan_todays_games",                        None, None),  # re-scan Sofascore for night games
    (14, 0,  "scan_and_save_odds",                       None, None),  # pull night game odds fresh at 2 PM
    (14, 0,  "scan_player_props",                        None, None),  # pull night props fresh at 2 PM
    (16, 30, "generate_hardrock_night_entry",            None, None),  # HardRock night entry
    (16, 35, "generate_prediction_market_night_entry",   None, None),  # Kalshi/Poly night entry (5 min after)
    (22, 0,  "send_daily_summary",      None, None),  # checks if last game done; skips if not
    (23, 0,  "send_daily_summary",      None, None),  # retry at 11 PM
    (0,  30, "send_daily_summary",      None, None),  # retry at 12:30 AM
    (1,  30, "send_daily_summary",      None, None),  # retry at 1:30 AM (late west coast finishes)
    (2,  30, "send_daily_summary",      None, None),  # final retry at 2:30 AM before sleep mode
    (0,  0,  "send_weekly_summary",     6,    None),  # Sunday
    (0,  5,  "send_weekly_fresh_start", 0,    None),  # Monday
]


def _run(fn, name: str):
    """Call a task function, swallowing exceptions so the loop never dies."""
    try:
        logger.info("► %s", name)
        result = fn()
        logger.info("✓ %s → %s", name, result)
    except Exception:
        logger.error("✗ %s failed:\n%s", name, traceback.format_exc())


def _cron_matches(hour: int, minute: int, day_of_week, day_of_month, now: datetime) -> bool:
    if now.hour != hour or now.minute != minute:
        return False
    if day_of_week is not None and now.weekday() != day_of_week:
        return False
    if day_of_month is not None and now.day != day_of_month:
        return False
    return True


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    logger.info("Sports Bot runner starting — loading tasks…")
    tasks = _import_tasks()
    logger.info("Tasks loaded: %s", sorted(tasks.keys()))

    # Track last-run time for interval tasks
    last_run: dict[str, float] = {name: 0.0 for _, name in INTERVAL_TASKS}
    # Track which cron minute was last fired to avoid double-firing
    last_cron_fired: dict[str, str] = {}

    # Graceful shutdown
    _running = [True]
    def _stop(sig, frame):
        logger.info("Shutdown signal received — stopping…")
        _running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("Runner loop started")

    while _running[0]:
        now   = datetime.now(ET)
        ts    = time.monotonic()
        now_key = now.strftime("%Y-%m-%d %H:%M")   # unique per minute

        # ── Interval tasks ──────────────────────────────────────────────────
        for interval, name in INTERVAL_TASKS:
            if ts - last_run[name] >= interval:
                fn = tasks.get(name)
                if fn:
                    _run(fn, name)
                last_run[name] = ts

        # ── Cron tasks ──────────────────────────────────────────────────────
        for hour, minute, name, dow, dom in CRON_TASKS:
            fired_key = f"{name}:{now_key}"
            if fired_key not in last_cron_fired and _cron_matches(hour, minute, dow, dom, now):
                fn = tasks.get(name)
                if fn:
                    _run(fn, name)
                last_cron_fired[fired_key] = now_key
                # Prune old keys to avoid unbounded growth
                if len(last_cron_fired) > 500:
                    oldest = sorted(last_cron_fired)[:-200]
                    for k in oldest:
                        del last_cron_fired[k]

        time.sleep(1)

    logger.info("Runner stopped cleanly")


if __name__ == "__main__":
    main()
