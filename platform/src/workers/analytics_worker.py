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

    # Fetch actual picks and results from DB for the summary
    from src.db.session import get_db
    from src.db.models import Pick
    from datetime import datetime, timedelta
    with get_db() as db:
        today_picks = db.query(
            Pick.selection, Pick.sport, Pick.american_odds_at_gen,
            Pick.ev_pct, Pick.units, Pick.recommendation,
        ).filter(
            Pick.generated_at >= datetime.utcnow() - timedelta(hours=24),
            Pick.recommendation == "BET",
        ).limit(20).all()
        settled = db.query(
            Pick.selection, Pick.sport, Pick.result, Pick.actual_pnl_units,
        ).filter(
            Pick.settled_at >= datetime.utcnow() - timedelta(hours=24),
            Pick.result.isnot(None),
        ).limit(20).all()

    picks_dicts = [
        {"bet": sel, "sport": sp, "odds": odds, "ev": ev, "units": u}
        for sel, sp, odds, ev, u, _ in today_picks
    ]
    results_dicts = [
        {"bet": sel, "sport": sp, "result": res, "pnl": pnl}
        for sel, sp, res, pnl in settled
    ]
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


@app.task
def cleanup_old_snapshots():
    """Delete OddsSnapshots older than 7 days — prevents unbounded table growth.

    Without cleanup: ~6.9M rows/day accumulate and eventually kill query performance.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, LineMovement, AlertRecord
    from datetime import datetime, timedelta

    cutoff_snapshots = datetime.utcnow() - timedelta(days=7)
    cutoff_alerts    = datetime.utcnow() - timedelta(days=30)

    with get_db() as db:
        deleted_snaps = db.query(OddsSnapshot).filter(
            OddsSnapshot.captured_at < cutoff_snapshots
        ).delete(synchronize_session=False)

        deleted_alerts = db.query(AlertRecord).filter(
            AlertRecord.sent_at < cutoff_alerts
        ).delete(synchronize_session=False)

    logger.info(
        "Cleanup: deleted %d old snapshots, %d old alert records",
        deleted_snaps, deleted_alerts,
    )
    return {"snapshots_deleted": deleted_snaps, "alerts_deleted": deleted_alerts}
