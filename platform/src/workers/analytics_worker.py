"""Analytics worker — daily/weekly summaries, self-improvement, portfolio snapshots."""
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)


@app.task
def send_daily_summary():
    from src.engines.summary_engine import get_daily_summary
    from src.engines.ai_engine import write_daily_summary
    from src.engines.clv_engine import aggregate_clv

    daily = get_daily_summary()
    clv_stats = aggregate_clv("daily")
    picks_dicts = []
    results_dicts = []
    summary = write_daily_summary(picks_dicts, results_dicts, clv_stats, bankroll=daily.get("net_units", 0))

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_daily_summary
    _run_async(post_daily_summary(summary))

    logger.info("Daily summary sent")
    return {"summary_length": len(summary), "stats": daily}


@app.task
def send_weekly_summary():
    from src.engines.summary_engine import get_weekly_summary
    from src.engines.ai_engine import write_weekly_summary

    weekly = get_weekly_summary()
    summary = write_weekly_summary(weekly)

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_weekly_summary
    _run_async(post_weekly_summary(summary))

    logger.info("Weekly summary sent")
    return {"summary_length": len(summary), "stats": weekly}


@app.task
def send_monthly_summary():
    from src.engines.summary_engine import get_monthly_summary
    from datetime import datetime

    now = datetime.utcnow()
    monthly = get_monthly_summary(year=now.year, month=now.month)

    lines = [
        f"**Monthly Summary — {now.strftime('%B %Y')}**",
        f"Total Bets: {monthly['total_bets']} | P&L: {monthly['total_profit']:+.2f}u | ROI: {monthly['total_roi']:.1%}",
        f"Best Sport: {monthly['best_sport'] or '—'} | Best Market: {monthly['best_market'] or '—'}",
        f"Avg CLV: {monthly['avg_clv']:.2%}",
        f"Win Streak: {monthly['largest_winning_streak']} | Loss Streak: {monthly['largest_losing_streak']}",
    ]
    summary = "\n".join(lines)

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import send_to_channel
    _run_async(send_to_channel("monthly-summary", content=summary))

    logger.info("Monthly summary sent")
    return {"summary_length": len(summary), "stats": monthly}


@app.task
def run_self_improvement():
    from src.engines.self_improvement_engine import run_full_self_improvement
    result = run_full_self_improvement()
    logger.info("Self-improvement cycle done: %s periods", len(result))
    return result


@app.task
def snapshot_portfolio():
    from src.engines.portfolio_engine import get_performance_stats
    from src.db.session import get_db
    from src.db.models import BankrollSnapshot
    from datetime import datetime

    stats = get_performance_stats("lifetime")
    daily_stats = get_performance_stats("daily")
    with get_db() as db:
        db.add(BankrollSnapshot(
            recorded_at  = datetime.utcnow(),
            balance      = stats.get("net_units", 0),
            units_total  = daily_stats.get("net_units", 0),
            note         = "auto",
        ))
    logger.info("Portfolio snapshot saved")
    return stats
