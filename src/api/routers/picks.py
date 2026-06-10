"""Picks endpoints — list, filter, trigger generation."""
from fastapi import APIRouter, Query, HTTPException
from pydantic import Field
from datetime import datetime, timedelta
from src.db.session import get_db
from src.db.models import Pick
from src.core.config import SPORTS
from src.core.timezone import et_naive

router = APIRouter()
_VALID_SPORTS = set(SPORTS.keys()) | set(SPORTS.values())


@router.get("/")
def list_picks(
    period: str = Query("daily", enum=["daily", "weekly", "monthly", "lifetime"]),
    sport: str | None = Query(None),
    recommendation: str | None = Query(None, enum=["BET", "PASS"]),
    limit: int = Query(50, ge=1, le=200),
):
    if sport and sport not in _VALID_SPORTS:
        raise HTTPException(status_code=400, detail=f"Unknown sport '{sport}'")

    cutoffs = {
        "daily":    et_naive() - timedelta(days=1),
        "weekly":   et_naive() - timedelta(weeks=1),
        "monthly":  et_naive() - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs[period]

    with get_db() as db:
        q = db.query(Pick).filter(Pick.generated_at >= cutoff)
        if sport:
            q = q.filter(Pick.sport == sport)
        if recommendation:
            q = q.filter(Pick.recommendation == recommendation.upper())
        rows = q.order_by(Pick.generated_at.desc()).limit(limit).all()
        # Access all fields inside session to avoid DetachedInstanceError
        result = [_pick_dict(p) for p in rows]

    return result


@router.get("/{pick_id}")
def get_pick(pick_id: int):
    with get_db() as db:
        p = db.query(Pick).filter_by(id=pick_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Pick not found")
        return _pick_dict(p)


@router.post("/generate")
def trigger_generation():
    """Manually trigger pick generation."""
    from src.workers.picks_worker import generate_picks
    result = generate_picks()
    return {"status": "completed", "result": result}


def _pick_dict(p: Pick) -> dict:
    return {
        "id":                   p.id,
        "sport":                p.sport,
        "selection":            p.selection,
        "market":               p.market,
        "best_book":            p.best_book,
        "american_odds_at_gen": p.american_odds_at_gen,
        "decimal_odds_at_gen":  p.decimal_odds_at_gen,
        "american_odds_close":  p.american_odds_close,
        "recommendation":       p.recommendation,
        "units":                p.units,
        "ev_pct":               p.ev_pct,
        "confidence_pct":       p.confidence_pct,
        "risk_score":           p.risk_score,
        "opportunity_score":    p.opportunity_score,
        "result":               p.result,
        "clv_pct":              p.clv_pct,
        "actual_pnl_units":     p.actual_pnl_units,
        "reasoning":            p.reasoning,
        "key_factors":          p.key_factors,
        "generated_at":         p.generated_at.isoformat() if p.generated_at else None,
        "settled_at":           p.settled_at.isoformat() if p.settled_at else None,
    }
