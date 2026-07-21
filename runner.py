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

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _RFH(
            "/var/log/sports-bot/runner.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,              # keep 5 rotated files → 50 MB max total
        ),
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
    logger.error("DB init failed — cannot start without schema: %s", _e)
    sys.exit(1)

# ── Task imports ───────────────────────────────────────────────────────────────

def _import_tasks():
    from src.workers.odds_worker             import scan_and_save_odds, scan_player_props, refresh_active_sports
    from src.workers.news_worker             import fetch_and_save_news
    from src.workers.picks_worker            import scan_todays_games, generate_hardrock_day_entry, generate_hardrock_night_entry, pre_research_day_entry, pre_research_night_entry
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
        check_odds_api_restored, health_check,
    )
    return {
        "scan_and_save_odds":                       scan_and_save_odds,
        "scan_player_props":                        scan_player_props,
        "refresh_active_sports":                    refresh_active_sports,
        "fetch_and_save_news":                      fetch_and_save_news,
        "scan_todays_games":                        scan_todays_games,
        "generate_hardrock_day_entry":              generate_hardrock_day_entry,
        "generate_hardrock_night_entry":            generate_hardrock_night_entry,
        "pre_research_day_entry":                   pre_research_day_entry,
        "pre_research_night_entry":                 pre_research_night_entry,
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
        "check_odds_api_restored":                  check_odds_api_restored,
        "health_check":                             health_check,
    }


# ── Schedule definition ────────────────────────────────────────────────────────
# interval tasks: run every N seconds
# cron tasks:     run at specific (hour, minute) ET — optionally day_of_week / day_of_month

INTERVAL_TASKS = [
    (30,   "track_slips"),            # 30 s — fast poll so results post the moment a game ends
    (1800, "scan_and_save_odds"),     # 30 min — credit-efficient (in-season sports only)
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
    (7,  0,  "check_odds_api_restored", None, 10),   # 10th of month — confirms Odds API credits reset
    (2,  0,  "run_self_improvement",    None, None),
    (2,  50, "send_weekly_summary",     6,    None),  # Sunday only
    (2,  57, "send_weekly_fresh_start", 6,    None),  # Sunday only — fresh start after summary
    (2,  52, "cleanup_old_slips",        None, None),
    (2,  55, "cleanup_old_snapshots",   None, None),
    (2,  58, "flush_memory",            None, None),
    (12, 0,  "health_check",            None, None),  # midday system health
    (12, 5,  "cleanup_old_snapshots",   None, None),  # midday prune — prevents rows piling up if 3 AM missed
    (18, 5,  "cleanup_old_snapshots",   None, None),  # evening prune — keeps DB lean on heavy game days
    (3,  0,  "enter_sleep_mode",        None, None),
    (3,  0,  "refresh_active_sports",  None, None),  # 3 AM wipe all stale caches — clean slate for new day
    (5,  0,  "wake_up_brief",            None, None),
    (6,  0,  "yesterday_recap",         None, None),  # every day
    (8,  0,  "scan_todays_games",                        None, None),  # 8 AM full Sofascore scan — worldwide day + night
    (8,  0,  "scan_and_save_odds",                       None, None),  # 8 AM HardRock odds pull
    (8,  0,  "scan_player_props",                        None, None),  # 8 AM Kalshi pricing pull
    (9,  0,  "scan_player_props",                        None, None),  # 9 AM Kalshi API pull — fresh markets before day slip
    (10, 0,  "pre_research_day_entry",                    None, None),  # 10:00 AM — AI research all day games
    (10, 30, "generate_hardrock_day_entry",               None, None),  # HardRock day entry
    (10, 35, "generate_prediction_market_day_entry",      None, None),  # Kalshi day entry
    (15, 0,  "scan_todays_games",                         None, None),  # 3 PM rescan — update night games not yet started
    (15, 0,  "scan_and_save_odds",                        None, None),  # 3 PM HardRock odds pull
    (15, 0,  "scan_player_props",                         None, None),  # 3 PM Kalshi pricing pull
    (16, 0,  "pre_research_night_entry",                  None, None),  # 4:00 PM — AI research all night games
    (16, 30, "generate_hardrock_night_entry",             None, None),  # HardRock night entry  4:30 PM ET
    (16, 35, "generate_prediction_market_night_entry",    None, None),  # Kalshi night entry    4:35 PM ET
    (18,  0, "scan_and_save_odds",                        None, None),  # 6 PM refresh — night game lines
    (18,  0, "scan_player_props",                         None, None),  # 6 PM props refresh for night games
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
    "generate_hardrock_night_entry":         360,  # 4:30–10:30 PM
    "generate_prediction_market_night_entry":360,  # 4:35–10:35 PM
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
    (8,  0,  120, "scan_and_save_odds"),
    (8,  0,  120, "scan_player_props"),
    (9,  0,   90, "scan_player_props"),
    (10, 0,   30, "pre_research_day_entry"),
    (10, 30, 180, "generate_hardrock_day_entry"),
    (10, 35, 180, "generate_prediction_market_day_entry"),
    (15, 0,   90, "scan_todays_games"),
    (15, 0,   90, "scan_and_save_odds"),
    (15, 0,   90, "scan_player_props"),
    (16, 0,   30, "pre_research_night_entry"),
    (16, 30, 360, "generate_hardrock_night_entry"),
    (16, 35, 360, "generate_prediction_market_night_entry"),
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
            # Also mark the exact-minute key so the cron loop won't double-fire
            last_cron_fired[f"{name}:{date_str} {hour:02d}:{minute:02d}"] = now.strftime("%Y-%m-%d %H:%M")


def main():
    logger.info("Sports Bot runner starting — loading tasks…")
    tasks = _import_tasks()
    logger.info("Tasks loaded: %s", sorted(tasks.keys()))

    # Validate all scheduled task names exist in the dispatch dict
    _all_scheduled = {name for _, name in INTERVAL_TASKS} | {name for _, _, name, _, _ in CRON_TASKS}
    _missing = _all_scheduled - tasks.keys()
    if _missing:
        logger.error("STARTUP ERROR — unknown task names in schedule: %s", sorted(_missing))
        sys.exit(1)

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

    # ── Startup: reload active slips from DB into Redis (survives restarts) ──────
    try:
        from src.workers.slip_tracker import reload_slips_from_db
        reloaded = reload_slips_from_db()
        if reloaded:
            logger.info("Startup: restored %d active slip(s) from DB into Redis", reloaded)
    except Exception as _se:
        logger.warning("Startup: slip reload failed: %s", _se)

    # ── One-time record reset (2026-07-08 fresh start) ───────────────────────
    try:
        from src.core.config import REDIS_URL
        import redis as _redis_mod
        _r_init = _redis_mod.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _reset_flag = "slips:record_reset:2026-07-08"
        if not _r_init.get(_reset_flag):
            from src.workers.slip_tracker import reset_record
            reset_record()
            _r_init.set(_reset_flag, "1")  # permanent — no TTL
            logger.info("Startup: record reset to 0W-0L (fresh start 2026-07-08)")
    except Exception as _re:
        logger.warning("Startup: record reset failed: %s", _re)

    # ── One-time correction: remove ghost win from Spain vs Belgium (July 9 slip) ──
    try:
        _ghost_fix_flag = "slips:ghost_win_fix:2026-07-10"
        if not _r_init.get(_ghost_fix_flag):
            from src.workers.slip_tracker import adjust_record
            adjust_record(wins_delta=-1)
            _r_init.set(_ghost_fix_flag, "1")  # permanent — runs once
            logger.info("Startup: removed 1 ghost win from Spain vs Belgium (July 9 slip)")
    except Exception as _re:
        logger.warning("Startup: ghost win correction failed: %s", _re)

    # ── Startup: refresh sports list then scan odds so catchup tasks have fresh data ──
    _startup_hour = datetime.now(ET).hour
    if 5 <= _startup_hour < 23:  # skip during dead hours (2:30–6 AM)
        # Refresh active sports FIRST — a mid-day restart finds a stale/empty Redis
        # sports list and scan_and_save_odds returns 0 events → no picks all day.
        logger.info("Startup: refreshing active sports...")
        if tasks.get("refresh_active_sports"):
            _run(tasks["refresh_active_sports"], "refresh_active_sports [startup]")
        logger.info("Startup: running initial odds scan before catchup...")
        if tasks.get("scan_and_save_odds"):
            _run_bg(tasks["scan_and_save_odds"], "scan_and_save_odds [startup]")
        last_run["scan_and_save_odds"] = time.monotonic()  # prevent immediate re-run

    # ── Catch-up: run any critical tasks missed due to restart ────────────────
    # Must run AFTER last_cron_fired is initialized so catchup marks tasks as fired.
    _run_catchup(tasks, datetime.now(ET), last_cron_fired)

    while _running[0]:
        now   = datetime.now(ET)
        ts    = time.monotonic()
        now_key = now.strftime("%Y-%m-%d %H:%M")   # unique per minute

        # ── Interval tasks ──────────────────────────────────────────────────
        # Skip interval tasks during sleep window (3–5 AM ET)
        # Exception: track_slips always runs so results post the moment a game ends
        _in_sleep = 3 <= now.hour < 5
        _SLEEP_EXEMPT = {"track_slips"}
        for interval, name in INTERVAL_TASKS:
            if _in_sleep and name not in _SLEEP_EXEMPT:
                last_run[name] = ts  # reset timer so tasks don't pile up at 5 AM
                continue
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

        # Prune old minute-keyed entries only (window date-keys must never be evicted)
        if len(last_cron_fired) > 500:
            _window_names = set(CRON_WINDOWS)
            pruneable = [k for k in last_cron_fired if k.split(":")[0] not in _window_names]
            for k in sorted(pruneable, key=lambda k: last_cron_fired[k])[:-200]:
                del last_cron_fired[k]

        time.sleep(1)

    logger.info("Runner stopped cleanly")


if __name__ == "__main__":
    main()
