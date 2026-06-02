"""Portfolio endpoints — daily portfolio, bankroll snapshots."""
from fastapi import APIRouter, Query
from src.engines.portfolio_engine import get_performance_stats

router = APIRouter()


@router.get("/summary")
def portfolio_summary(period: str = Query("daily", enum=["daily", "weekly", "monthly", "lifetime"])):
    return get_performance_stats(period)


@router.get("/bankroll-history")
def bankroll_history(days: int = Query(30, le=365)):
    from src.db.session import get_db
    from src.db.models import BankrollSnapshot
    from datetime import datetime, timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_db() as db:
        rows = db.query(BankrollSnapshot).filter(
            BankrollSnapshot.snapshot_date >= cutoff.date()
        ).order_by(BankrollSnapshot.snapshot_date.asc()).all()

    return [
        {
            "date": str(r.snapshot_date),
            "bankroll": r.bankroll,
            "daily_pnl": r.daily_pnl,
        }
        for r in rows
    ]


@router.post("/snapshot")
def trigger_snapshot():
    from src.workers.analytics_worker import snapshot_portfolio
    task = snapshot_portfolio.delay()
    return {"task_id": task.id, "status": "queued"}
