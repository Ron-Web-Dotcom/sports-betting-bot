"""
Engine 13 — Self-Improvement Engine.

Tracks ROI by every dimension (sport, market, book, unit level, signal type).
Recalibrates confidence scores based on actual vs predicted performance.
Identifies which categories are performing and adjusts weights accordingly.
"""
import logging
from datetime import datetime, timedelta
from src.db.session import get_db
from src.db.models import Pick, PerformanceMetrics

logger = logging.getLogger(__name__)

DIMENSIONS = ["sport", "market", "best_book", "units", "result"]


def compute_roi_by_dimension(dimension: str, period: str = "lifetime") -> list[dict]:
    """
    Compute performance metrics grouped by a dimension.
    dimension: "sport" | "market" | "best_book" | "units"
    period: "daily" | "weekly" | "monthly" | "lifetime"
    """
    cutoffs = {
        "daily":    datetime.utcnow() - timedelta(days=1),
        "weekly":   datetime.utcnow() - timedelta(weeks=1),
        "monthly":  datetime.utcnow() - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs.get(period, cutoffs["lifetime"])

    col_map = {
        "sport":     Pick.sport,
        "market":    Pick.market,
        "best_book": Pick.best_book,
        "units":     Pick.units,
    }
    col = col_map.get(dimension, Pick.sport)

    with get_db() as db:
        pick_rows = db.query(
            Pick.result, Pick.actual_pnl_units, Pick.units, Pick.ev_pct,
            Pick.confidence_pct, col,
        ).filter(
            Pick.generated_at >= cutoff,
            Pick.result.in_(["won", "lost", "push"]),
            Pick.recommendation == "BET",
        ).all()

    if not pick_rows:
        return []

    # Group by dimension value (last column in each row)
    groups: dict = {}
    for row in pick_rows:
        result, pnl, units, ev, conf, dim_val = row
        key = str(dim_val) if dim_val is not None else "unknown"
        groups.setdefault(key, []).append(
            {"result": result, "actual_pnl_units": pnl, "units": units,
             "ev_pct": ev, "confidence_pct": conf}
        )

    results = []
    for key, group in groups.items():
        wins   = [p for p in group if p["result"] == "won"]
        losses = [p for p in group if p["result"] == "lost"]
        units_won  = sum(p["actual_pnl_units"] or 0 for p in wins)
        units_lost = sum(abs(p["actual_pnl_units"] or 0) for p in losses)

        total_wagered = sum(p["units"] or 1 for p in group)
        results.append({
            "dimension":       dimension,
            "value":           key,
            "total":           len(group),
            "wins":            len(wins),
            "losses":          len(losses),
            "units_won":       round(units_won, 2),
            "units_lost":      round(units_lost, 2),
            "net_units":       round(units_won - units_lost, 2),
            "total_wagered":   round(total_wagered, 2),
            "hit_rate":        round(len(wins) / len(group), 4),
            "roi":             round((units_won - units_lost) / max(total_wagered, 0.01), 4),
            "avg_ev":          round(sum(p["ev_pct"] or 0 for p in group) / len(group), 4),
            "avg_confidence":  round(sum(p["confidence_pct"] or 0 for p in group) / len(group), 2),
        })

    results.sort(key=lambda x: x["roi"], reverse=True)
    return results


def persist_metrics(metrics: list[dict], period: str) -> None:
    with get_db() as db:
        for m in metrics:
            existing = db.query(PerformanceMetrics).filter_by(
                dimension=m["dimension"],
                dimension_value=str(m["value"]),
                period=period,
            ).first()
            if existing:
                existing.total_picks   = m["total"]
                existing.wins          = m["wins"]
                existing.losses        = m["losses"]
                existing.units_won     = m["units_won"]
                existing.units_lost    = m["units_lost"]
                existing.roi_pct       = m["roi"]
                existing.avg_ev_pct    = m["avg_ev"]
                existing.hit_rate      = m["hit_rate"]
                existing.profit_factor = m["units_won"] / max(m["units_lost"], 0.01)
                existing.updated_at    = datetime.utcnow()
            else:
                db.add(PerformanceMetrics(
                    dimension       = m["dimension"],
                    dimension_value = str(m["value"]),
                    period          = period,
                    period_start    = datetime.utcnow(),
                    total_picks     = m["total"],
                    wins            = m["wins"],
                    losses          = m["losses"],
                    units_won       = m["units_won"],
                    units_lost      = m.get("units_lost", 0),
                    roi_pct         = m["roi"],
                    avg_ev_pct      = m["avg_ev"],
                    hit_rate        = m["hit_rate"],
                    profit_factor   = m["units_won"] / max(m.get("units_lost", 0.01), 0.01),
                ))


def run_full_self_improvement() -> dict:
    """
    Full self-improvement cycle — runs all dimensions for all periods.
    Called nightly by Celery.
    """
    logger.info("Running self-improvement cycle…")
    summary: dict = {}

    for period in ["daily", "weekly", "monthly", "lifetime"]:
        for dim in ["sport", "market", "best_book", "units"]:
            metrics = compute_roi_by_dimension(dim, period)
            if metrics:
                persist_metrics(metrics, period)
                summary.setdefault(period, {})[dim] = metrics[:3]   # top 3 per dimension

    logger.info("Self-improvement cycle complete")
    return summary


def get_top_performing_categories(limit: int = 5) -> dict:
    """Returns the top performing categories by ROI across all dimensions."""
    with get_db() as db:
        rows = db.query(PerformanceMetrics).filter(
            PerformanceMetrics.period == "lifetime",
            PerformanceMetrics.total_picks >= 10,
        ).order_by(PerformanceMetrics.roi_pct.desc()).limit(limit).all()
        return {
            f"{r.dimension}:{r.dimension_value}": {
                "roi": r.roi_pct, "picks": r.total_picks, "hit_rate": r.hit_rate,
            }
            for r in rows
        }
