"""
Engine 12 — Portfolio Management Engine.

Builds daily portfolios categorised by risk tier.
Enforces daily exposure limits across the full portfolio.
Tracks P&L, ROI, drawdown at portfolio level.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from src.core.config import MAX_DAILY_UNITS, MAX_SPORT_EXPOSURE_PCT

logger = logging.getLogger(__name__)


@dataclass
class DailyPortfolio:
    date:          str
    safe_picks:    list[dict] = field(default_factory=list)    # units 1-2
    medium_picks:  list[dict] = field(default_factory=list)    # units 3
    high_picks:    list[dict] = field(default_factory=list)    # units 4-5
    parlay_picks:  list[dict] = field(default_factory=list)

    @property
    def total_units(self) -> float:
        all_picks = self.safe_picks + self.medium_picks + self.high_picks
        return sum(p.get("units", 0) for p in all_picks)

    @property
    def sport_exposure(self) -> dict[str, float]:
        all_picks = self.safe_picks + self.medium_picks + self.high_picks
        exp: dict[str, float] = {}
        for p in all_picks:
            exp[p.get("sport", "?")] = exp.get(p.get("sport", "?"), 0) + p.get("units", 0)
        return exp

    @property
    def expected_roi(self) -> float:
        all_picks = self.safe_picks + self.medium_picks + self.high_picks
        if not all_picks:
            return 0.0
        return sum(p.get("ev_pct", 0) * p.get("units", 0) for p in all_picks) / max(self.total_units, 1)

    def summary(self) -> str:
        return (
            f"Portfolio {self.date}: "
            f"{len(self.safe_picks)} safe / {len(self.medium_picks)} medium / {len(self.high_picks)} high risk | "
            f"{self.total_units:.1f}u total | Expected ROI: {self.expected_roi:.1%}"
        )


def build_daily_portfolio(all_picks: list[dict]) -> DailyPortfolio:
    """
    Takes all BET picks for today and categorises them by risk tier.
    Enforces MAX_DAILY_UNITS hard cap.
    """
    portfolio = DailyPortfolio(date=date.today().isoformat())
    units_used = 0.0
    sport_units: dict[str, float] = {}

    # Sort by opportunity_score descending — best picks first
    sorted_picks = sorted(all_picks, key=lambda p: p.get("opportunity_score", 0), reverse=True)

    for pick in sorted_picks:
        units   = pick.get("units", 0)
        sport   = pick.get("sport", "?")
        total_u = units_used + units

        if total_u > MAX_DAILY_UNITS:
            logger.info("Daily unit limit reached — skipping pick: %s", pick.get("bet", ""))
            continue

        # Sport concentration check — only enforce when portfolio has multiple sports
        sport_u = sport_units.get(sport, 0) + units
        if units_used > 0 and sport_u / max(total_u, 1) > MAX_SPORT_EXPOSURE_PCT:
            logger.info("Sport concentration limit for %s — skipping", sport)
            continue

        # Categorise
        if units <= 2:
            portfolio.safe_picks.append(pick)
        elif units == 3:
            portfolio.medium_picks.append(pick)
        else:
            portfolio.high_picks.append(pick)

        units_used = total_u
        sport_units[sport] = sport_units.get(sport, 0) + units

    return portfolio


def get_performance_stats(period: str = "lifetime") -> dict:
    """Aggregate performance across all settled picks."""
    from src.db.session import get_db
    from src.db.models import Pick
    from datetime import timedelta

    cutoffs = {
        "daily":    datetime.utcnow() - timedelta(days=1),
        "weekly":   datetime.utcnow() - timedelta(weeks=1),
        "monthly":  datetime.utcnow() - timedelta(days=30),
        "lifetime": datetime(2000, 1, 1),
    }
    cutoff = cutoffs.get(period, cutoffs["lifetime"])

    with get_db() as db:
        rows = db.query(
            Pick.result, Pick.actual_pnl_units, Pick.units,
        ).filter(
            Pick.generated_at >= cutoff,
            Pick.result.in_(["won", "lost", "push"]),
        ).all()

    if not rows:
        return {"period": period, "total": 0, "wins": 0, "losses": 0,
                "units_won": 0.0, "roi": 0.0, "hit_rate": 0.0}

    wins   = [r for r in rows if r.result == "won"]
    losses = [r for r in rows if r.result == "lost"]
    units_won    = sum(r.actual_pnl_units or 0 for r in wins)
    units_lost   = sum(abs(r.actual_pnl_units or 0) for r in losses)
    total_wagered = sum(r.units or 1 for r in rows)

    return {
        "period":        period,
        "total":         len(rows),
        "wins":          len(wins),
        "losses":        len(losses),
        "units_won":     round(units_won,  2),
        "units_lost":    round(units_lost, 2),
        "net_units":     round(units_won - units_lost, 2),
        "total_wagered": round(total_wagered, 2),
        "hit_rate":      round(len(wins) / len(rows), 4),
        "roi":           round((units_won - units_lost) / max(total_wagered, 0.01), 4),
        "profit_factor": round(units_won / max(units_lost, 0.01), 2),
    }


def persist_portfolio(portfolio: DailyPortfolio) -> None:
    from src.db.session import get_db
    from src.db.models import DailyPortfolio as DBPortfolio
    with get_db() as db:
        existing = db.query(DBPortfolio).filter_by(date=portfolio.date).first()
        if existing:
            existing.safe_pick_ids   = [p.get("id") for p in portfolio.safe_picks]
            existing.medium_pick_ids = [p.get("id") for p in portfolio.medium_picks]
            existing.high_pick_ids   = [p.get("id") for p in portfolio.high_picks]
            existing.total_units     = portfolio.total_units
            existing.expected_roi    = portfolio.expected_roi
        else:
            db.add(DBPortfolio(
                date           = portfolio.date,
                safe_pick_ids  = [p.get("id") for p in portfolio.safe_picks],
                medium_pick_ids= [p.get("id") for p in portfolio.medium_picks],
                high_pick_ids  = [p.get("id") for p in portfolio.high_picks],
                total_units    = portfolio.total_units,
                expected_roi   = portfolio.expected_roi,
            ))
