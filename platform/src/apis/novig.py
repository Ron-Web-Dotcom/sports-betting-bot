"""
Novig adapter.

Novig is a no-vig peer-to-peer betting exchange. Because there's no house
edge, odds reflect true market consensus — making Novig lines valuable as
a sharp reference for EV calculations.

Set env var: NOVIG_API_KEY  (from novig.us dashboard)
API docs: https://docs.novig.us
"""
import logging
import os

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.novig.us/v1"

# Maps our sport_key → Novig sport slug
_SPORT_MAP: dict[str, str] = {
    "americanfootball_nfl":   "american_football",
    "basketball_nba":         "basketball",
    "baseball_mlb":           "baseball",
    "icehockey_nhl":          "ice_hockey",
    "basketball_ncaab":       "basketball",
    "americanfootball_ncaaf": "american_football",
    "soccer_epl":             "soccer",
    "soccer_usa_mls":         "soccer",
    "tennis_atp_french_open": "tennis",
    "mma_mixed_martial_arts": "mma",
    "golf_masters_tournament_winner": "golf",
}


def _key() -> str:
    return os.getenv("NOVIG_API_KEY", "").strip()


def _get(path: str, params: dict | None = None) -> dict | list | None:
    key = _key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        return get_json(f"{_BASE}{path}", params=params, headers=headers)
    except Exception as e:
        logger.warning("Novig GET %s failed: %s", path, e)
        return None


def get_markets(sport_key: str | None = None) -> list[dict]:
    """
    Fetch open betting markets from Novig exchange.
    Returns normalised list with no-vig true odds.
    """
    key = _key()
    if not key:
        logger.debug("NOVIG_API_KEY not set — skipping")
        return []

    params: dict = {"status": "open"}
    sport_slug = _SPORT_MAP.get(sport_key or "", "")
    if sport_slug:
        params["sport"] = sport_slug

    data = _get("/markets", params)
    if not data:
        return []

    raw = data if isinstance(data, list) else data.get("markets", data.get("data", []))
    out = []
    for m in raw:
        outcomes = m.get("outcomes", [])
        for outcome in outcomes:
            price = outcome.get("price") or outcome.get("odds")
            if price is None:
                continue
            # Novig may return decimal or american format
            if isinstance(price, float) and price < 20:
                # likely decimal odds — convert
                american = _decimal_to_american(price)
            else:
                american = int(price)

            out.append({
                "market_id":  m.get("id", ""),
                "event":      m.get("event", m.get("name", "")),
                "market":     m.get("market_type", m.get("type", "h2h")),
                "selection":  outcome.get("name", outcome.get("label", "")),
                "american":   american,
                "decimal":    outcome.get("decimal_price", _american_to_decimal(american)),
                "no_vig":     True,
                "sport":      sport_slug,
                "sport_key":  sport_key or "",
                "game_time":  m.get("commence_time", m.get("start_time", "")),
                "source":     "novig",
            })

    logger.info("Novig: %d outcomes for %s", len(out), sport_key or "all")
    return out


def get_all_markets() -> list[dict]:
    """Fetch all open markets across all tracked sports."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(get_markets, sk): sk for sk in _SPORT_MAP}
        for future in as_completed(futures, timeout=20):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.warning("Novig fetch failed for %s: %s", futures[future], e)
    return results


def _decimal_to_american(decimal: float) -> int:
    if decimal <= 1.0:
        return -10000
    if decimal >= 2.0:
        return int((decimal - 1) * 100)
    return int(-100 / (decimal - 1))


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return round(american / 100 + 1, 4)
    return round(100 / abs(american) + 1, 4)
