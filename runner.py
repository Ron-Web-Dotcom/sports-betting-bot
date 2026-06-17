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

# Run DB column migrations before anything else
try:
    from src.db.session import init_db
    init_db()
    logger.info("DB init/migrations complete")
except Exception as _e:
    logger.warning("DB init failed (non-fatal): %s", _e)

# ── Task imports ───────────────────────────────────────────────────────────────

def _import_tasks():
    from src.workers.odds_worker             import scan_and_save_odds, scan_player_props, refresh_active_sports
    from src.workers.news_worker             import fetch_and_save_news
    from src.workers.picks_worker            import scan_todays_games, generate_hardrock_day_entry, generate_hardrock_night_entry
    from src.workers.prediction_market_worker import (
        generate_prediction_market_day_entry,
        generate_prediction_market_night_entry,
    )
    from src.workers.settlement_worker import settle_completed_picks, record_closing_lines
    from src.workers.slip_tracker import track_slips
    from src.workers.analytics_worker import (
        enter_sleep_mode, wake_up_brief, send_daily_summary,
        send_weekly_summary, send_weekly_fresh_start, run_self_improvement,
        snapshot_portfolio, send_monthly_summary, yesterday_recap,
        cleanup_old_snapshots, flush_memory, cleanup_old_slips,
    )
    return {
        "scan_and_save_odds":                       scan_and_save_odds,
        "scan_player_props":                        scan_player_props,
        "refresh_active_sports":                    refresh_active_sports,
        "fetch_and_save_news":                      fetch_and_save_news,
        "scan_todays_games":                        scan_todays_games,
        "generate_hardrock_day_entry":              generate_hardrock_day_entry,
        "generate_hardrock_night_entry":            generate_hardrock_night_entry,
        "generate_prediction_market_day_entry":     generate_prediction_market_day_entry,
        "generate_prediction_market_night_entry":   generate_prediction_market_night_entry,
        "settle_completed_picks":                   settle_completed_picks,
        "record_closing_lines":                     record_closing_lines,
        "track_slips":                              track_slips,
        "enter_sleep_mode":                         enter_sleep_mode,
        "wake_up_brief":                            wake_up_brief,
        "send_daily_summary":                       send_daily_summary,
        "send_weekly_summary":                      send_weekly_summary,
        "send_weekly_fresh_start":                  send_weekly_fresh_start,
        "run_self_improvement":                     run_self_improvement,
        "snapshot_portfolio":                       snapshot_portfolio,
        "send_monthly_summary":                     send_monthly_summary,
        "yesterday_recap":                          yesterday_recap,
        "cleanup_old_snapshots":                    cleanup_old_snapshots,
        "flush_memory":                             flush_memory,
        "cleanup_old_slips":                        cleanup_old_slips,
    }


# ── Schedule definition ────────────────────────────────────────────────────────
# interval tasks: run every N seconds
# cron tasks:     run at specific (hour, minute) ET — optionally day_of_week / day_of_month

INTERVAL_TASKS = [
    (180,  "track_slips"),            # 3 min — slip lifecycle alerts
    (600,  "scan_and_save_odds"),     # 10 min — keep odds fresh
    (1200, "scan_player_props"),      # 20 min — player + team props for HardRock entries
    (1800, "fetch_and_save_news"),    # 30 min
    (1800, "settle_completed_picks"), # 30 min
    (3600, "record_closing_lines"),   # 60 min
]

CRON_TASKS = [
    # (hour, minute, task_name, day_of_week=None, day_of_month=None)
    # day_of_week: 0=Monday … 6=Sunday  (Python weekday())
    (0,  5,  "snapshot_portfolio",      None, None),
    (0,  1,  "send_monthly_summary",    None, 1),     # 1st of month
    (2,  0,  "run_self_improvement",    None, None),
    (2,  52, "cleanup_old_slips",        None, None),
    (2,  55, "cleanup_old_snapshots",   None, None),
    (2,  58, "flush_memory",            None, None),
    (3,  0,  "enter_sleep_mode",        None, None),
    (5,  0,  "wake_up_brief",            None, None),
    (5,  30, "refresh_active_sports",   None, None),  # refresh after wake, before first scan
    (6,  0,  "yesterday_recap",         1,    None),  # Tuesday
    (6,  0,  "yesterday_recap",         2,    None),  # Wednesday
    (6,  0,  "yesterday_recap",         3,    None),  # Thursday
    (6,  0,  "yesterday_recap",         4,    None),  # Friday
    (6,  0,  "yesterday_recap",         5,    None),  # Saturday
    (6,  0,  "yesterday_recap",         6,    None),  # Sunday
    (8,  0,  "scan_todays_games",               None, None),  # Sofascore full scan — split day/night, cache
    (10, 30, "generate_hardrock_day_entry",               None, None),  # HardRock day entry
    (10, 35, "generate_prediction_market_day_entry",     None, None),  # Kalshi/Poly day entry (5 min after)
    (14, 0,  "scan_todays_games",                        None, None),  # re-scan Sofascore for night games
    (14, 0,  "scan_and_save_odds",                       None, None),  # pull night game odds fresh at 2 PM
    (16, 30, "generate_hardrock_night_entry",            None, None),  # HardRock night entry
    (16, 35, "generate_prediction_market_night_entry",   None, None),  # Kalshi/Poly night entry (5 min after)
]


def _run(fn, name: str):
    """Call a task function, swallowing exceptions so the loop never dies."""
    try:
        logger.info("► %s", name)
        result = fn()
        logger.info("✓ %s → %s", name, result)
    except Exception:
        logger.error("✗ %s failed:\n%s", name, traceback.format_exc())


# Heavy tasks run in background threads so they never block the cron scheduler.
_BACKGROUND_TASKS = {
    "scan_and_save_odds",
    "scan_player_props",
    "fetch_and_save_news",
    "settle_completed_picks",
    "record_closing_lines",
    "run_self_improvement",
    "cleanup_old_snapshots",
    "flush_memory",
    "cleanup_old_slips",
}


def _run_bg(fn, name: str):
    """Run a heavy task in a daemon thread — never blocks the scheduler."""
    import threading
    t = threading.Thread(target=_run, args=(fn, name), daemon=True, name=name)
    t.start()


# Tasks with a posting window — fire anytime within N minutes after scheduled time.
# Redis dedup prevents double-posting within the same window.
CRON_WINDOWS = {
    "generate_hardrock_day_entry":           60,  # 10:30–11:30 AM
    "generate_prediction_market_day_entry":  60,  # 10:35–11:35 AM
    "generate_hardrock_night_entry":         60,  # 4:30–5:30 PM
    "generate_prediction_market_night_entry":60,  # 4:35–5:35 PM
}


def _cron_matches(hour: int, minute: int, day_of_week, day_of_month, now: datetime) -> bool:
    if day_of_week is not None and now.weekday() != day_of_week:
        return False
    if day_of_month is not None and now.day != day_of_month:
        return False
    # Check exact minute match for regular tasks
    return now.hour == hour and now.minute == minute


# ── Main loop ──────────────────────────────────────────────────────────────────

# Tasks that must not be missed if the runner restarts mid-day.
# If runner starts within the catch-up window after the scheduled time, run immediately.
# Redis dedup prevents double-posting for hardrock entries.
CATCHUP_TASKS = [
    # (hour, minute, catch_up_window_minutes, task_name)
    (8,  0,  120, "scan_todays_games"),
    (10, 30, 180, "generate_hardrock_day_entry"),
    (10, 35, 180, "generate_prediction_market_day_entry"),
    (14, 0,  120, "scan_todays_games"),
    (16, 30, 180, "generate_hardrock_night_entry"),
    (16, 35, 180, "generate_prediction_market_night_entry"),
]


def _run_catchup(tasks: dict, now: datetime, last_cron_fired: dict):
    """On startup: run any critical task whose window was missed in the last N minutes."""
    date_str = now.strftime("%Y-%m-%d")
    for hour, minute, window, name in CATCHUP_TASKS:
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elapsed   = (now - scheduled).total_seconds() / 60  # minutes since scheduled time
        if 0 < elapsed <= window:
            # Mark as fired so the cron loop won't fire it again this session
            date_key = f"{name}:{date_str}"
            if date_key in last_cron_fired:
                logger.info("CATCH-UP: %s already fired today — skipping", name)
                continue
            logger.info("CATCH-UP: %s was scheduled at %02d:%02d, missed by %.0f min — running now", name, hour, minute, elapsed)
            fn = tasks.get(name)
            if fn:
                if name in _BACKGROUND_TASKS:
                    _run_bg(fn, f"{name} [catch-up]")
                else:
                    _run(fn, f"{name} [catch-up]")
            last_cron_fired[date_key] = now.strftime("%Y-%m-%d %H:%M")


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

    # ── Catch-up: run any critical tasks missed due to restart ────────────────
    # Must run AFTER last_cron_fired is initialized so catchup marks tasks as fired.
    _run_catchup(tasks, datetime.now(ET), last_cron_fired)

    while _running[0]:
        now   = datetime.now(ET)
        ts    = time.monotonic()
        now_key = now.strftime("%Y-%m-%d %H:%M")   # unique per minute

        # ── Interval tasks ──────────────────────────────────────────────────
        for interval, name in INTERVAL_TASKS:
            if ts - last_run[name] >= interval:
                fn = tasks.get(name)
                if fn:
                    if name in _BACKGROUND_TASKS:
                        _run_bg(fn, name)
                    else:
                        _run(fn, name)
                last_run[name] = ts

        # ── Cron tasks ──────────────────────────────────────────────────────
        for hour, minute, name, dow, dom in CRON_TASKS:
            window = CRON_WINDOWS.get(name, 0)
            if window:
                # Window-based: fire anytime within N minutes after scheduled time
                scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                elapsed   = (now - scheduled).total_seconds() / 60
                in_window = 0 <= elapsed <= window
                day_ok    = (dow is None or now.weekday() == dow) and (dom is None or now.day == dom)
                date_key  = f"{name}:{now.strftime('%Y-%m-%d')}"
                if in_window and day_ok and date_key not in last_cron_fired:
                    fn = tasks.get(name)
                    if fn:
                        _run(fn, name)
                    last_cron_fired[date_key] = now_key
            else:
                fired_key = f"{name}:{now_key}"
                if fired_key not in last_cron_fired and _cron_matches(hour, minute, dow, dom, now):
                    fn = tasks.get(name)
                    if fn:
                        if name in _BACKGROUND_TASKS:
                            _run_bg(fn, name)
                        else:
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
