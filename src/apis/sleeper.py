"""Sleeper public API — player data, rosters, matchups."""
import logging
import httpx
from config.settings import SLEEPER_API_BASE

logger = logging.getLogger(__name__)

SPORT_LEAGUE_MAP = {
    "nfl": "nfl",
    "nba": "nba",
    "mlb": "mlb",
}


def _get(path: str) -> dict | list | None:
    try:
        resp = httpx.get(f"{SLEEPER_API_BASE}{path}", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("Sleeper error %s: %s", path, exc)
        return None


def get_players(sport: str = "nfl") -> dict:
    """Returns full player dict keyed by player_id."""
    data = _get(f"/players/{sport}")
    return data or {}


def get_trending_players(sport: str = "nfl", trend_type: str = "add", limit: int = 25) -> list[dict]:
    data = _get(f"/players/{sport}/trending/{trend_type}?limit={limit}")
    return data or []


def get_nfl_state() -> dict:
    data = _get("/state/nfl")
    return data or {}


def get_player_news(sport: str = "nfl") -> list[dict]:
    """Approximate news via trending adds — Sleeper has no dedicated news endpoint."""
    trending = get_trending_players(sport, trend_type="add", limit=50)
    players  = get_players(sport)
    enriched = []
    for item in trending:
        pid = item.get("player_id")
        player = players.get(pid, {})
        enriched.append(
            {
                "player_id":   pid,
                "player_name": f"{player.get('first_name','')} {player.get('last_name','')}".strip(),
                "team":        player.get("team", ""),
                "position":    player.get("position", ""),
                "status":      player.get("status", ""),
                "injury_status": player.get("injury_status", ""),
                "count":       item.get("count", 0),
                "sport":       sport,
            }
        )
    return enriched
