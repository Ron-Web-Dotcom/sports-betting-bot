"""
PrizePicks adapter.

PrizePicks is a player props platform offering Over/Under lines across
NFL, NBA, MLB, NHL, Soccer, Tennis, UFC, Golf, NCAAB, NCAAF, Esports, and more.

Public API — no key required.
Base: https://api.prizepicks.com

Key endpoints:
  GET /projections?league_id={id}&per_page=250   — all active props for a league
  GET /leagues                                    — all available leagues + IDs
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.prizepicks.com"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://app.prizepicks.com/",
}

# Maps our internal sport_key → PrizePicks league_id
# IDs sourced from their /leagues endpoint
_LEAGUE_IDS: dict[str, int] = {
    "basketball_nba":           7,
    "americanfootball_nfl":     9,
    "baseball_mlb":             2,
    "icehockey_nhl":            6,
    "basketball_ncaab":         3,
    "americanfootball_ncaaf":   8,
    "soccer_epl":               14,
    "soccer_usa_mls":           14,
    "tennis_atp_french_open":   16,
    "mma_mixed_martial_arts":   10,
    "golf_masters_tournament_winner": 12,
    "esports_lol":              15,
}


def _get(path: str, params: dict | None = None) -> dict | list | None:
    try:
        return get_json(f"{_BASE}{path}", params=params, headers=_HEADERS)
    except Exception as e:
        logger.warning("PrizePicks GET %s failed: %s", path, e)
        return None


def get_leagues() -> list[dict]:
    """Return all active leagues with their IDs."""
    data = _get("/leagues")
    if not data:
        return []
    leagues = data if isinstance(data, list) else data.get("data", [])
    return [
        {
            "id":      l.get("id"),
            "name":    l.get("attributes", {}).get("name", ""),
            "sport":   l.get("attributes", {}).get("sport", ""),
            "active":  l.get("attributes", {}).get("active", False),
            "source":  "prizepicks",
        }
        for l in leagues
    ]


def get_projections(sport_key: str) -> list[dict]:
    """
    Fetch all active player prop projections for a sport.
    Returns normalised list with player, stat, line, and team.
    """
    league_id = _LEAGUE_IDS.get(sport_key)
    if not league_id:
        # Try all leagues for unmapped sports
        logger.debug("PrizePicks: no league_id mapped for %s", sport_key)
        return []

    data = _get("/projections", {"league_id": league_id, "per_page": 250, "single_stat": True})
    if not data:
        return []

    # PrizePicks returns JSON:API format — data + included
    raw_projections = data.get("data", []) if isinstance(data, dict) else []
    included = {
        item["id"]: item
        for item in data.get("included", [])
        if isinstance(data, dict)
    }

    out = []
    for proj in raw_projections:
        attrs = proj.get("attributes", {})
        relationships = proj.get("relationships", {})

        # Resolve player from included
        player_rel = relationships.get("new_player", {}).get("data", {})
        player_id = player_rel.get("id")
        player_obj = included.get(player_id, {})
        player_attrs = player_obj.get("attributes", {})

        player_name = (
            attrs.get("name")
            or player_attrs.get("name", "Unknown")
        )
        team = player_attrs.get("team", "") or attrs.get("team", "")

        line = attrs.get("line_score")
        stat = attrs.get("stat_type", "")
        game_time = attrs.get("start_time", "")
        opponent = attrs.get("opponent_name", "")
        status = attrs.get("status", "")

        if line is None or not stat:
            continue

        out.append({
            "player":      player_name,
            "team":        team,
            "opponent":    opponent,
            "stat":        stat,
            "line":        float(line),
            "game_time":   game_time,
            "status":      status,
            "sport_key":   sport_key,
            "league_id":   league_id,
            "source":      "prizepicks",
        })

    logger.info("PrizePicks: %d projections for %s", len(out), sport_key)
    return out


def get_all_projections() -> list[dict]:
    """Fetch projections for every mapped sport in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(get_projections, sport_key): sport_key
            for sport_key in _LEAGUE_IDS
        }
        for future in as_completed(futures, timeout=20):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.warning("PrizePicks fetch failed for %s: %s", futures[future], e)
    return results


def find_prop(sport_key: str, player_name: str, stat: str) -> dict | None:
    """Find a specific player prop by name and stat type."""
    name_lower = player_name.lower()
    stat_lower = stat.lower()
    for proj in get_projections(sport_key):
        if (name_lower in proj["player"].lower()
                and stat_lower in proj["stat"].lower()):
            return proj
    return None
