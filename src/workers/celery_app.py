"""
Celery application — Sports Intelligence Platform.

6 queues: odds, news, picks, alerts, settlement, analytics.
"""
import logging
from celery import Celery
from celery.schedules import crontab
from src.core.config import CELERY_BROKER, CELERY_BACKEND

# Silence noisy third-party loggers — 429/403 errors are handled at source level
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai._base_client").setLevel(logging.WARNING)
logging.getLogger("tenacity").setLevel(logging.WARNING)

app = Celery(
    "sports_intel",
    broker=CELERY_BROKER,
    backend=CELERY_BACKEND,
    include=[
        "src.workers.odds_worker",
        "src.workers.news_worker",
        "src.workers.picks_worker",
        "src.workers.alert_worker",
        "src.workers.settlement_worker",
        "src.workers.analytics_worker",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/New_York",   # user is Eastern time
    enable_utc=True,
    task_routes={
        "src.workers.odds_worker.*":       {"queue": "odds"},
        "src.workers.news_worker.*":       {"queue": "news"},
        "src.workers.picks_worker.*":      {"queue": "picks"},
        "src.workers.alert_worker.*":      {"queue": "alerts"},
        "src.workers.settlement_worker.*": {"queue": "settlement"},
        "src.workers.analytics_worker.*":  {"queue": "analytics"},
    },
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=600,   # SIGTERM at 10 min — lets task clean up
    task_time_limit=900,        # SIGKILL at 15 min — settlement may hit multiple sport APIs
)

app.conf.beat_schedule = {
    # ── Live scanning — skips automatically during sleep window (3 AM–5 AM ET) ──
    # Odds (ML/spread/totals) — every 5 min
    "scan-odds-every-5min": {
        "task": "src.workers.odds_worker.scan_and_save_odds",
        "schedule": 300,
    },
    # Player props scan — every 5 min
    "scan-props-every-5min": {
        "task": "src.workers.odds_worker.scan_player_props",
        "schedule": 300,
    },
    # News / injuries — every 15 min
    "fetch-news-every-15min": {
        "task": "src.workers.news_worker.fetch_and_save_news",
        "schedule": 900,
    },
    # Pick generation — every 10 min
    "generate-picks-every-10min": {
        "task": "src.workers.picks_worker.generate_picks",
        "schedule": 600,
    },
    # Prop picks — every 5 min, single summary post
    "prop-picks-every-5min": {
        "task": "src.workers.picks_worker.scan_and_pick_props",
        "schedule": 300,
    },
    # Pre-game alerts — every minute
    "pregame-alerts-every-min": {
        "task": "src.workers.alert_worker.send_pregame_alerts",
        "schedule": 60,
    },

    # ── Sleep / wake cycle (Eastern time) ──────────────────────────────────────
    # 3:00 AM ET — enter sleep mode
    "sleep-mode-3am": {
        "task": "src.workers.analytics_worker.enter_sleep_mode",
        "schedule": crontab(hour=3, minute=0),
    },
    # 5:00 AM ET — wake up + today's games brief
    "wake-up-5am": {
        "task": "src.workers.analytics_worker.wake_up_brief",
        "schedule": crontab(hour=5, minute=0),
    },
    # 9:30 AM ET — first combined picks summary of the day
    "morning-picks-930am": {
        "task": "src.workers.picks_worker.morning_picks_summary",
        "schedule": crontab(hour=9, minute=30),
    },

    # ── Settlement & analytics ─────────────────────────────────────────────────
    # Settlement — every 30 min
    "settle-picks-every-30min": {
        "task": "src.workers.settlement_worker.settle_completed_picks",
        "schedule": 1800,
    },
    # CLV closing snapshots — hourly
    "record-clv-hourly": {
        "task": "src.workers.settlement_worker.record_closing_lines",
        "schedule": 3600,
    },
    # Daily summary — 11 PM Eastern
    "daily-summary": {
        "task": "src.workers.analytics_worker.send_daily_summary",
        "schedule": crontab(hour=23, minute=0),
    },
    # Weekly summary — Sunday midnight Eastern
    "weekly-summary": {
        "task": "src.workers.analytics_worker.send_weekly_summary",
        "schedule": crontab(day_of_week=0, hour=0, minute=0),
    },
    # Monday fresh start — 12:05 AM Eastern
    "weekly-fresh-start": {
        "task": "src.workers.analytics_worker.send_weekly_fresh_start",
        "schedule": crontab(day_of_week=1, hour=0, minute=5),
    },
    # Self-improvement — nightly 2 AM Eastern (runs during sleep window, no alert)
    "self-improvement-nightly": {
        "task": "src.workers.analytics_worker.run_self_improvement",
        "schedule": crontab(hour=2, minute=0),
    },
    # Portfolio snapshot — midnight Eastern
    "portfolio-snapshot": {
        "task": "src.workers.analytics_worker.snapshot_portfolio",
        "schedule": crontab(hour=0, minute=5),
    },
    # Monthly summary — 1st of month 12:01 AM Eastern
    "monthly-summary": {
        "task": "src.workers.analytics_worker.send_monthly_summary",
        "schedule": crontab(day_of_month=1, hour=0, minute=1),
    },
    # 6:00 AM ET — yesterday's record + today's game count
    "yesterday-recap-6am": {
        "task": "src.workers.analytics_worker.yesterday_recap",
        "schedule": crontab(hour=6, minute=0),
    },
    # OddsSnapshot cleanup — 3 AM Eastern (fires just before sleep mode kicks in)
    "cleanup-old-snapshots": {
        "task": "src.workers.analytics_worker.cleanup_old_snapshots",
        "schedule": crontab(hour=2, minute=55),
    },
    # Today's RECAP Summary — 2:59 AM Eastern (fires just before sleep mode)
    "todays-recap-259am": {
        "task": "src.workers.analytics_worker.todays_recap",
        "schedule": crontab(hour=2, minute=59),
    },
}
