"""
Betr adapter.

Betr is a micro-betting and parlay-focused sportsbook covering NFL, NBA,
MLB, NHL, UFC, Tennis, Golf, and NCAAF/NCAAB.

Betr does not publish a public API. This adapter uses their unofficial
public endpoints discovered via network inspection. No auth required for
reading odds.

Base: https://api.betr.app
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.betr.app"
_HEADERS = {
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":     "https://www.betr.app/",
    "Accept":      "application/json",
}

# Maps our sport_key → Betr sport slug
_SPORT_MAP: dict[str, str] = {
    "americanfootball_nfl":           "american-football",
    "basketball_nba":                 "basketball",
    "baseball_mlb":                   "baseball",
    "icehockey_nhl":                  "ice-hockey",
    "basketball_ncaab":               "college-basketball",
    "americanfootball_ncaaf":         "college-football",
    "mma_mixed_martial_arts":         "mma",
    "tennis_atp_french_open":         "tennis",
    "golf_masters_tournament_winner": "golf",
}


def _get(path: str) -> dict | list | None:
    try:
        return get_json(f"{_BASE}{path}", headers=_HEADERS)
    except Exception as e:
        logger.debug("Betr GET %s failed: %s", path, e)
        return None


def get_events(sport_key: str) -> list[dict]:
    """
    Fetch upcoming events and odds from Betr for a given sport.
    Returns normalised list with ML, spread, totals where available.
    """
    slug = _SPORT_MAP.get(sport_key)
    if not slug:
        return []

    # Try common unofficial endpoint patterns
    data = (
        _get(f"/v1/sports/{slug}/events")
        or _get(f"/sports/{slug}/events")
        or _get(f"/api/sports/{slug}/markets")
    )
    if not data:
        logger.debug("Betr: no data for %s", sport_key)
        return []

    events_raw = data if isinstance(data, list) else (
        data.get("events") or data.get("data") or data.get("markets") or []
    )

    out = []
    for e in events_raw:
        home = e.get("home_team") or e.get("homeTeam") or e.get("home", "")
        away = e.get("away_team") or e.get("awayTeam") or e.get("away", "")
        game_time = e.get("start_time") or e.get("startTime") or e.get("commence_time", "")

        for mkt in (e.get("markets") or e.get("odds") or []):
            mkt_type = mkt.get("type") or mkt.get("market_type", "h2h")
            for outcome in (mkt.get("outcomes") or mkt.get("selections") or []):
                price = outcome.get("price") or outcome.get("odds") or outcome.get("american_odds")
                if price is None:
                    continue
                out.append({
                    "event":      f"{away} vs {home}",
                    "home_team":  home,
                    "away_team":  away,
                    "market":     mkt_type,
                    "selection":  outcome.get("name") or outcome.get("label", ""),
                    "american":   int(price) if isinstance(price, (int, float)) else 0,
                    "game_time":  game_time,
                    "sport_key":  sport_key,
                    "source":     "betr",
                })

    logger.info("Betr: %d outcomes for %s", len(out), sport_key)
    return out


def get_all_events() -> list[dict]:
    """Fetch events for all mapped sports."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(get_events, sk): sk for sk in _SPORT_MAP}
        for future in as_completed(futures, timeout=20):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.debug("Betr fetch failed: %s", e)
    return results
