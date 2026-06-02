"""The Odds API — live lines for all tracked sports."""
import logging
import httpx
from config.settings import ODDS_API_KEY, ODDS_API_BASE, TRACKED_SPORTS

logger = logging.getLogger(__name__)

MARKETS = ["h2h", "spreads", "totals"]


def _get(path: str, params: dict) -> dict | list | None:
    params["apiKey"] = ODDS_API_KEY
    try:
        resp = httpx.get(f"{ODDS_API_BASE}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("OddsAPI error %s: %s", path, exc)
        return None


def get_sports() -> list[dict]:
    result = _get("/sports", {"all": "true"})
    return result or []


def get_odds(sport: str, markets: list[str] | None = None) -> list[dict]:
    """Return live odds events for a sport."""
    mkts = ",".join(markets or MARKETS)
    result = _get(
        f"/sports/{sport}/odds",
        {"regions": "us", "markets": mkts, "oddsFormat": "american"},
    )
    return result or []


def get_scores(sport: str, days_from: int = 1) -> list[dict]:
    result = _get(f"/sports/{sport}/scores", {"daysFrom": days_from})
    return result or []


def get_event_odds(sport: str, event_id: str) -> dict | None:
    result = _get(
        f"/sports/{sport}/events/{event_id}/odds",
        {"regions": "us", "markets": ",".join(MARKETS), "oddsFormat": "american"},
    )
    return result


def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1
    return 100 / abs(american) + 1


def implied_prob(american: int) -> float:
    dec = american_to_decimal(american)
    return 1 / dec


def fetch_all_odds() -> dict[str, list[dict]]:
    """Pull odds for every tracked sport. Returns {sport: [events]}."""
    results = {}
    for sport in TRACKED_SPORTS:
        events = get_odds(sport)
        if events:
            results[sport] = events
            logger.info("OddsAPI: %d events for %s", len(events), sport)
    return results
