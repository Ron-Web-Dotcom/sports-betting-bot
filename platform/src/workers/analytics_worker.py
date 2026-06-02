"""Analytics worker — daily/weekly summaries, self-improvement, portfolio snapshots."""
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)


@app.task
def send_daily_summary():
    from src.engines.portfolio_engine import get_performance_stats
    from src.engines.clv_engine import aggregate_clv
    from src.engines.ai_engine import write_daily_summary
    from src.db.session import get_db
    from src.db.models import Pick
    from datetime import datetime, timedelta

    stats = get_performance_stats("daily")
    clv_stats = aggregate_clv("daily")

    with get_db() as db:
        picks = db.query(Pick).filter(
            Pick.generated_at >= datetime.utcnow() - timedelta(days=1),
            Pick.recommendation == "BET",
        ).limit(20).all()

    picks_dicts = [{"bet": p.selection, "sport": p.sport, "result": p.result,
                    "units": p.units, "ev_pct": p.ev_pct} for p in picks]
    results_dicts = [{"bet": p.selection, "result": p.result,
                      "pnl": p.actual_pnl_units} for p in picks if p.result]

    summary = write_daily_summary(picks_dicts, results_dicts, clv_stats, bankroll=stats.get("net_units", 0))

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_daily_summary
    _run_async(post_daily_summary(summary))

    logger.info("Daily summary sent")
    return {"summary_length": len(summary)}


@app.task
def send_weekly_summary():
    from src.engines.portfolio_engine import get_performance_stats
    from src.engines.ai_engine import write_weekly_summary

    stats = get_performance_stats("weekly")
    summary = write_weekly_summary(stats)

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_weekly_summary
    _run_async(post_weekly_summary(summary))

    logger.info("Weekly summary sent")
    return {"summary_length": len(summary)}


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
    with get_db() as db:
        db.add(BankrollSnapshot(
            snapshot_date=datetime.utcnow().date(),
            bankroll=stats.get("net_units", 0),
            daily_pnl=get_performance_stats("daily").get("net_units", 0),
            notes="auto",
        ))
    logger.info("Portfolio snapshot saved")
    return stats
