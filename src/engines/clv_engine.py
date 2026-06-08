"""
Engine 9 — Closing Line Value Engine.

CLV = how much better (or worse) our odds were vs closing odds.
Positive CLV proves long-term skill — we're consistently beating the efficient market.
"""
import logging
from datetime import datetime, timedelta
from src.engines.ev_engine import implied_prob

logger = logging.getLogger(__name__)


def calculate_clv(odds_at_pick: int, odds_at_close: int) -> float:
    """
    CLV in probability terms.
    Positive = we got better odds than closing = skill signal.
    e.g. We bet -110, closed at -130 → we beat the market.
    """
    p_pick  = implied_prob(odds_at_pick)
    p_close = implied_prob(odds_at_close)
    return round(p_close - p_pick, 4)   # positive = beat closing line


def clv_to_american_delta(odds_at_pick: int, odds_at_close: int) -> int:
    return odds_at_pick - odds_at_close   # neg = close moved against us


def record_clv(pick_id: int, odds_at_pick: int, odds_at_close: int) -> None:
    from sqlalchemy.exc import IntegrityError
    from src.db.session import get_db
    from src.db.models import CLVRecord, Pick
    clv = calculate_clv(odds_at_pick, odds_at_close)
    with get_db() as db:
        # Update pick record
        pick = db.query(Pick).filter_by(id=pick_id).first()
        if pick:
            pick.american_odds_close = odds_at_close
            pick.clv_pct             = clv

        # Upsert CLV record using a savepoint so a conflict doesn't poison
        # the outer session (db.rollback() would roll back the pick update too).
        existing = db.query(CLVRecord).filter_by(pick_id=pick_id).first()
        if existing:
            existing.odds_at_close    = odds_at_close
            existing.implied_at_close = implied_prob(odds_at_close)
            existing.clv_pct          = clv
            existing.recorded_at      = datetime.utcnow()
        else:
            rec = CLVRecord(
                pick_id         = pick_id,
                sport           = pick.sport if pick else "",
                market          = pick.market if pick else "",
                book            = pick.best_book if pick else "",
                odds_at_pick    = odds_at_pick,
                odds_at_close   = odds_at_close,
                implied_at_pick = implied_prob(odds_at_pick),
                implied_at_close= implied_prob(odds_at_close),
                clv_pct         = clv,
            )
            db.add(rec)
            try:
                # Use a savepoint: rollback only undoes the INSERT, not the
                # preceding pick update, keeping the outer transaction intact.
                with db.begin_nested():
                    db.flush()
            except IntegrityError:
                # Race condition: another process inserted first — update instead
                existing = db.query(CLVRecord).filter_by(pick_id=pick_id).first()
                if existing:
                    existing.odds_at_close    = odds_at_close
                    existing.implied_at_close = implied_prob(odds_at_close)
                    existing.clv_pct          = clv
                    existing.recorded_at      = datetime.utcnow()


def aggregate_clv(period: str = "daily", sport: str | None = None) -> dict:
    """
    Aggregate CLV stats for a period.
    period: 'daily' | 'weekly' | 'monthly' | 'lifetime'
    """
    from src.db.session import get_db
    from src.db.models import CLVRecord

    now = datetime.utcnow()
    cutoffs = {
        "daily":    now - timedelta(days=1),
        "weekly":   now - timedelta(weeks=1),
        "monthly":  now - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs.get(period, cutoffs["daily"])

    with get_db() as db:
        q = db.query(CLVRecord).filter(CLVRecord.recorded_at >= cutoff)
        if sport:
            q = q.filter(CLVRecord.sport == sport)
        records = q.all()
        clvs = [r.clv_pct for r in records]  # extract inside session

    if not clvs:
        return {"period": period, "sport": sport, "count": 0, "avg_clv": 0.0, "positive_pct": 0.0, "total_clv": 0.0}

    positive = sum(1 for c in clvs if c > 0)
    return {
        "period":       period,
        "sport":        sport,
        "count":        len(clvs),
        "avg_clv":      round(sum(clvs) / len(clvs), 4),
        "positive_pct": round(positive / len(clvs), 4),
        "total_clv":    round(sum(clvs), 4),
    }


def clv_by_dimension(dimension: str = "sport", period: str = "lifetime") -> dict:
    """CLV broken down by a given dimension (sport, market, book), filtered by period."""
    from src.db.session import get_db
    from src.db.models import CLVRecord
    from sqlalchemy import func

    now = datetime.utcnow()
    cutoffs = {
        "daily":    now - timedelta(days=1),
        "weekly":   now - timedelta(weeks=1),
        "monthly":  now - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs.get(period, cutoffs["lifetime"])

    valid_dims = {"sport", "market", "book"}
    dims = [dimension] if dimension in valid_dims else list(valid_dims)

    result: dict = {}
    with get_db() as db:
        for dim in dims:
            col = getattr(CLVRecord, dim)
            q = db.query(col, func.avg(CLVRecord.clv_pct), func.count(CLVRecord.id))\
                  .filter(CLVRecord.recorded_at >= cutoff)\
                  .group_by(col)
            rows = q.all()
            result[dim] = {
                row[0]: {"avg_clv": round(row[1] or 0, 4), "count": row[2]}
                for row in rows if row[0]
            }
    return result
