"""Summary engine — daily, weekly, monthly performance summaries."""
from datetime import datetime, timedelta
from typing import Optional


def get_daily_summary(date: Optional[datetime] = None) -> dict:
    from src.db.session import get_db
    from src.db.models import Pick

    date = date or datetime.utcnow()
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    with get_db() as db:
        picks = db.query(Pick).filter(
            Pick.generated_at >= start,
            Pick.generated_at < end,
            Pick.recommendation == "BET",
        ).all()
        # Read all fields inside session to avoid DetachedInstanceError
        pick_rows = [
            {
                "id": p.id, "sport": p.sport, "selection": p.selection,
                "market": p.market, "best_book": p.best_book,
                "result": p.result, "units": p.units or 0,
                "ev_pct": p.ev_pct or 0.0, "confidence_pct": p.confidence_pct or 0.0,
                "clv_pct": p.clv_pct, "actual_pnl_units": p.actual_pnl_units,
            }
            for p in picks
        ]

    total_picks = len(pick_rows)
    wins   = sum(1 for p in pick_rows if p["result"] == "won")
    losses = sum(1 for p in pick_rows if p["result"] == "lost")
    pushes = sum(1 for p in pick_rows if p["result"] == "push")
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    units_won  = sum(p["actual_pnl_units"] for p in pick_rows if p["result"] == "won" and p["actual_pnl_units"])
    units_lost = abs(sum(p["actual_pnl_units"] for p in pick_rows if p["result"] == "lost" and p["actual_pnl_units"]))
    net_units  = sum(p["actual_pnl_units"] for p in pick_rows if p["actual_pnl_units"] is not None)
    net_profit = net_units
    settled_rows = [p for p in pick_rows if p["result"] in ("won", "lost", "push")]
    total_wagered = sum(p["units"] for p in settled_rows if p["units"])
    roi = net_units / total_wagered if total_wagered > 0 else 0.0
    avg_ev         = sum(p["ev_pct"] for p in pick_rows) / total_picks if total_picks > 0 else 0.0
    avg_confidence = sum(p["confidence_pct"] for p in pick_rows) / total_picks if total_picks > 0 else 0.0
    clv_rows = [p for p in pick_rows if p["clv_pct"] is not None]
    avg_clv  = sum(p["clv_pct"] for p in clv_rows) / len(clv_rows) if clv_rows else 0.0

    settled_picks = [p for p in pick_rows if p["actual_pnl_units"] is not None]
    best_pick = worst_pick = None
    if settled_picks:
        bp = max(settled_picks, key=lambda p: p["actual_pnl_units"] or 0)
        wp = min(settled_picks, key=lambda p: p["actual_pnl_units"] or 0)
        best_pick  = {"id": bp["id"], "selection": bp["selection"], "sport": bp["sport"], "pnl": bp["actual_pnl_units"]}
        worst_pick = {"id": wp["id"], "selection": wp["selection"], "sport": wp["sport"], "pnl": wp["actual_pnl_units"]}

    sport_pnl: dict = {}
    for p in settled_picks:
        sport_pnl.setdefault(p["sport"], 0.0)
        sport_pnl[p["sport"]] += p["actual_pnl_units"] or 0
    best_sport  = max(sport_pnl, key=sport_pnl.get) if sport_pnl else None
    worst_sport = min(sport_pnl, key=sport_pnl.get) if sport_pnl else None

    book_pnl: dict = {}
    for p in settled_picks:
        book_pnl.setdefault(p["best_book"], 0.0)
        book_pnl[p["best_book"]] += p["actual_pnl_units"] or 0
    best_sportsbook = max(book_pnl, key=book_pnl.get) if book_pnl else None

    market_pnl: dict = {}
    for p in settled_picks:
        market_pnl.setdefault(p["market"], 0.0)
        market_pnl[p["market"]] += p["actual_pnl_units"] or 0
    most_profitable_market = max(market_pnl, key=market_pnl.get) if market_pnl else None

    return {
        "total_picks": total_picks, "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate": win_rate, "units_won": units_won, "units_lost": units_lost,
        "net_units": net_units, "net_profit": net_profit, "roi": roi,
        "avg_ev": avg_ev, "avg_confidence": avg_confidence, "avg_clv": avg_clv,
        "best_pick": best_pick, "worst_pick": worst_pick,
        "best_sport": best_sport, "worst_sport": worst_sport,
        "best_sportsbook": best_sportsbook, "most_profitable_market": most_profitable_market,
    }


def get_weekly_summary(week_start: Optional[datetime] = None) -> dict:
    from src.db.session import get_db
    from src.db.models import Pick

    if week_start is None:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)

    with get_db() as db:
        picks = db.query(Pick).filter(
            Pick.generated_at >= week_start,
            Pick.generated_at < week_end,
            Pick.recommendation == "BET",
        ).all()
        pick_rows = [
            {
                "sport": p.sport, "market": p.market, "best_book": p.best_book,
                "units": p.units or 0, "result": p.result,
                "actual_pnl_units": p.actual_pnl_units, "clv_pct": p.clv_pct,
                "is_parlay_leg": p.is_parlay_leg,
            }
            for p in picks
        ]

    total_bets = len(pick_rows)
    settled = [p for p in pick_rows if p["actual_pnl_units"] is not None]
    total_units_won  = sum(p["actual_pnl_units"] for p in settled if (p["actual_pnl_units"] or 0) > 0)
    total_units_lost = abs(sum(p["actual_pnl_units"] for p in settled if (p["actual_pnl_units"] or 0) < 0))
    net_units      = sum(p["actual_pnl_units"] for p in settled)
    net_profit     = net_units
    total_wagered  = sum(p["units"] for p in pick_rows if p["units"])
    roi = net_units / total_wagered if total_wagered > 0 else 0.0
    clv_picks = [p for p in pick_rows if p["clv_pct"] is not None]
    avg_clv   = sum(p["clv_pct"] for p in clv_picks) / len(clv_picks) if clv_picks else 0.0

    sport_pnl: dict = {}
    for p in settled:
        sport_pnl.setdefault(p["sport"], 0.0)
        sport_pnl[p["sport"]] += p["actual_pnl_units"] or 0
    best_sport  = max(sport_pnl, key=sport_pnl.get) if sport_pnl else None
    worst_sport = min(sport_pnl, key=sport_pnl.get) if sport_pnl else None

    market_pnl: dict = {}
    for p in settled:
        market_pnl.setdefault(p["market"], 0.0)
        market_pnl[p["market"]] += p["actual_pnl_units"] or 0
    best_market = max(market_pnl, key=market_pnl.get) if market_pnl else None

    book_pnl: dict = {}
    for p in settled:
        book_pnl.setdefault(p["best_book"], 0.0)
        book_pnl[p["best_book"]] += p["actual_pnl_units"] or 0
    best_sportsbook = max(book_pnl, key=book_pnl.get) if book_pnl else None

    prop_markets = [p for p in settled if "prop" in (p["market"] or "").lower()]
    prop_market_pnl: dict = {}
    for p in prop_markets:
        prop_market_pnl.setdefault(p["market"], 0.0)
        prop_market_pnl[p["market"]] += p["actual_pnl_units"] or 0
    best_prop_category = max(prop_market_pnl, key=prop_market_pnl.get) if prop_market_pnl else None

    parlay_picks = [p for p in settled if p["is_parlay_leg"]]
    parlay_sport_pnl: dict = {}
    for p in parlay_picks:
        parlay_sport_pnl.setdefault(p["sport"], 0.0)
        parlay_sport_pnl[p["sport"]] += p["actual_pnl_units"] or 0
    best_parlay_category = max(parlay_sport_pnl, key=parlay_sport_pnl.get) if parlay_sport_pnl else None

    return {
        "total_bets": total_bets,
        "total_units_won": total_units_won,
        "total_units_lost": total_units_lost,
        "net_units": net_units,
        "net_profit": net_profit,
        "roi": roi,
        "avg_clv": avg_clv,
        "best_sport": best_sport,
        "worst_sport": worst_sport,
        "best_market": best_market,
        "best_sportsbook": best_sportsbook,
        "best_prop_category": best_prop_category,
        "best_parlay_category": best_parlay_category,
    }


def get_monthly_summary(year: Optional[int] = None, month: Optional[int] = None) -> dict:
    from src.db.session import get_db
    from src.db.models import Pick, Parlay
    import calendar

    now = datetime.utcnow()
    year = year or now.year
    month = month or now.month
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59)

    with get_db() as db:
        picks = db.query(Pick).filter(
            Pick.generated_at >= start,
            Pick.generated_at <= end,
            Pick.recommendation == "BET",
        ).all()
        parlays = db.query(Parlay).filter(
            Parlay.created_at >= start,
            Parlay.created_at <= end,
        ).all()

    total_bets = len(picks)
    settled = [p for p in picks if p.actual_pnl_units is not None]
    total_profit = sum(p.actual_pnl_units for p in settled)
    total_units = sum(p.units for p in picks if p.units)
    total_roi = total_profit / total_units if total_units > 0 else 0.0
    clv_picks = [p for p in picks if p.clv_pct is not None]
    avg_clv = sum(p.clv_pct for p in clv_picks) / len(clv_picks) if clv_picks else 0.0

    sport_pnl: dict = {}
    for p in settled:
        sport_pnl.setdefault(p.sport, 0.0)
        sport_pnl[p.sport] += p.actual_pnl_units
    best_sport = max(sport_pnl, key=sport_pnl.get) if sport_pnl else None

    market_pnl: dict = {}
    for p in settled:
        market_pnl.setdefault(p.market, 0.0)
        market_pnl[p.market] += p.actual_pnl_units
    best_market = max(market_pnl, key=market_pnl.get) if market_pnl else None

    book_pnl: dict = {}
    for p in settled:
        book_pnl.setdefault(p.best_book, 0.0)
        book_pnl[p.best_book] += p.actual_pnl_units
    best_sportsbook = max(book_pnl, key=book_pnl.get) if book_pnl else None

    # parlays sorted by pnl
    settled_parlays = sorted(
        [pl for pl in parlays if pl.pnl_units is not None],
        key=lambda pl: pl.pnl_units,
        reverse=True,
    )
    best_parlays = [
        {"id": pl.id, "legs": len(pl.legs), "odds": pl.combined_odds, "pnl": pl.pnl_units}
        for pl in settled_parlays[:5]
    ]

    # best props
    prop_picks = sorted(
        [p for p in settled if "prop" in (p.market or "").lower()],
        key=lambda p: p.actual_pnl_units,
        reverse=True,
    )
    best_props = [
        {"id": p.id, "selection": p.selection, "sport": p.sport, "pnl": p.actual_pnl_units}
        for p in prop_picks[:5]
    ]

    # streaks
    picks_sorted = sorted(
        [p for p in picks if p.result in ("won", "lost") and p.settled_at],
        key=lambda p: p.settled_at,
    )
    largest_winning_streak = 0
    largest_losing_streak = 0
    cur_win = 0
    cur_loss = 0
    for p in picks_sorted:
        if p.result == "won":
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        largest_winning_streak = max(largest_winning_streak, cur_win)
        largest_losing_streak = max(largest_losing_streak, cur_loss)

    return {
        "total_profit": total_profit,
        "total_roi": total_roi,
        "total_bets": total_bets,
        "total_units": total_units,
        "avg_clv": avg_clv,
        "best_sport": best_sport,
        "best_market": best_market,
        "best_parlays": best_parlays,
        "best_props": best_props,
        "best_sportsbook": best_sportsbook,
        "largest_winning_streak": largest_winning_streak,
        "largest_losing_streak": largest_losing_streak,
    }
