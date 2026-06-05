"""
Celery application — Sports Intelligence Platform.

6 queues: odds, news, picks, alerts, settlement, analytics.
"""
from celery import Celery
from celery.schedules import crontab
from src.core.config import CELERY_BROKER, CELERY_BACKEND

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
    timezone="UTC",
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
    # Odds (ML/spread/totals) — every 5 min 24/7
    "scan-odds-every-5min": {
        "task": "src.workers.odds_worker.scan_and_save_odds",
        "schedule": 300,
    },
    # Player props (PrizePicks + Underdog) — every 10 min 24/7
    "scan-props-every-10min": {
        "task": "src.workers.odds_worker.scan_player_props",
        "schedule": 600,
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
    # Pre-game alerts — every minute
    "pregame-alerts-every-min": {
        "task": "src.workers.alert_worker.send_pregame_alerts",
        "schedule": 60,
    },
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
    # Daily summary — 11 PM UTC
    "daily-summary": {
        "task": "src.workers.analytics_worker.send_daily_summary",
        "schedule": crontab(hour=23, minute=0),
    },
    # Weekly summary — Sunday 11 PM UTC
    "weekly-summary": {
        "task": "src.workers.analytics_worker.send_weekly_summary",
        "schedule": crontab(day_of_week=0, hour=23, minute=30),
    },
    # Self-improvement — nightly 2 AM UTC
    "self-improvement-nightly": {
        "task": "src.workers.analytics_worker.run_self_improvement",
        "schedule": crontab(hour=2, minute=0),
    },
    # Portfolio snapshot — midnight UTC
    "portfolio-snapshot": {
        "task": "src.workers.analytics_worker.snapshot_portfolio",
        "schedule": crontab(hour=0, minute=5),
    },
    # Monthly summary — 1st of month 12:01 AM UTC
    "monthly-summary": {
        "task": "src.workers.analytics_worker.send_monthly_summary",
        "schedule": crontab(day_of_month=1, hour=0, minute=1),
    },
    # OddsSnapshot cleanup — nightly 3 AM: delete snapshots older than 7 days
    # Without this, ~6.9M rows/day accumulate and eventually kill the DB
    "cleanup-old-snapshots": {
        "task": "src.workers.analytics_worker.cleanup_old_snapshots",
        "schedule": crontab(hour=3, minute=0),
    },
    # Parlay generation — daily at 9 AM after picks are in
    "generate-parlays-daily": {
        "task": "src.workers.picks_worker.generate_parlays",
        "schedule": crontab(hour=9, minute=0),
    },
}
