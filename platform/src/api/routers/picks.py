"""Picks endpoints — list, filter, trigger generation."""
from fastapi import APIRouter, Query
from datetime import datetime, timedelta
from src.db.session import get_db
from src.db.models import Pick

router = APIRouter()


@router.get("/")
def list_picks(
    period: str = Query("daily", enum=["daily", "weekly", "monthly", "lifetime"]),
    sport: str | None = None,
    recommendation: str | None = Query(None, enum=["BET", "PASS"]),
    limit: int = Query(50, le=200),
):
    cutoffs = {
        "daily":    datetime.utcnow() - timedelta(days=1),
        "weekly":   datetime.utcnow() - timedelta(weeks=1),
        "monthly":  datetime.utcnow() - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs[period]

    with get_db() as db:
        q = db.query(Pick).filter(Pick.generated_at >= cutoff)
        if sport:
            q = q.filter(Pick.sport == sport)
        if recommendation:
            q = q.filter(Pick.recommendation == recommendation)
        picks = q.order_by(Pick.generated_at.desc()).limit(limit).all()

    return [_pick_dict(p) for p in picks]


@router.get("/{pick_id}")
def get_pick(pick_id: int):
    with get_db() as db:
        pick = db.query(Pick).filter_by(id=pick_id).first()
    if not pick:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Pick not found")
    return _pick_dict(pick)


@router.post("/generate")
def trigger_generation():
    """Manually trigger pick generation (enqueues Celery task)."""
    from src.workers.picks_worker import generate_picks
    task = generate_picks.delay()
    return {"task_id": task.id, "status": "queued"}


def _pick_dict(p: Pick) -> dict:
    return {
        "id": p.id,
        "sport": p.sport,
        "selection": p.selection,
        "best_book": p.best_book,
        "odds": p.american_odds_at_gen,
        "recommendation": p.recommendation,
        "units": p.units,
        "ev_pct": p.ev_pct,
        "confidence_pct": p.confidence_pct,
        "risk_score": p.risk_score,
        "opportunity_score": p.opportunity_score,
        "result": p.result,
        "actual_pnl_units": p.actual_pnl_units,
        "reasoning": p.reasoning,
        "key_factors": p.key_factors,
        "generated_at": p.generated_at.isoformat() if p.generated_at else None,
    }
