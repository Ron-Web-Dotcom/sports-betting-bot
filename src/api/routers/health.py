from fastapi import APIRouter
from src.core.timezone import et_naive

router = APIRouter()


@router.get("/")
def health_check():
    return {"status": "ok", "timestamp": et_naive().isoformat()}


@router.get("/system")
def system_health():
    from src.engines.health_engine import get_system_health
    health = get_system_health()
    return {
        "overall": health.overall,
        "timestamp": health.timestamp.isoformat(),
        "services": [
            {
                "name": s.name,
                "status": s.status,
                "latency_ms": s.latency_ms,
                "last_check": s.last_check.isoformat() if s.last_check else None,
                "error": s.error,
            }
            for s in health.services
        ],
    }


@router.get("/games/today")
def games_today():
    """Return all games with odds stored in DB for today (ET)."""
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.core.timezone import et_naive
        now = et_naive()
        today = now.strftime("%Y-%m-%d")
        snaps = get_latest_snapshots_by_game()
        games = {}
        for snap in snaps:
            key = f"{snap.get('away_team')} @ {snap.get('home_team')}"
            if key not in games:
                games[key] = {
                    "sport":       snap.get("sport_key"),
                    "home":        snap.get("home_team"),
                    "away":        snap.get("away_team"),
                    "commence":    snap.get("commence_time"),
                    "markets":     {},
                }
            mkt = snap.get("market", "h2h")
            sel = snap.get("selection", "")
            if mkt not in games[key]["markets"]:
                games[key]["markets"][mkt] = {}
            games[key]["markets"][mkt][sel] = snap.get("best_odds")
        return {
            "date":       today,
            "timestamp":  now.isoformat(),
            "game_count": len(games),
            "games":      list(games.values()),
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/kalshi/today")
def kalshi_today():
    """Return all active Kalshi markets fetched today."""
    try:
        from src.apis.kalshi import get_sports_markets
        from src.core.timezone import et_naive
        now = et_naive()
        markets = get_sports_markets()
        out = []
        for m in markets:
            out.append({
                "title":      m.get("title"),
                "sport":      m.get("sport_key"),
                "ticker":     m.get("ticker"),
                "yes_price":  m.get("yes_price"),
                "no_price":   m.get("no_price"),
                "volume":     m.get("volume"),
                "close_time": m.get("close_time"),
            })
        return {
            "timestamp":    now.isoformat(),
            "market_count": len(out),
            "markets":      out,
        }
    except Exception as e:
        return {"error": str(e)}
