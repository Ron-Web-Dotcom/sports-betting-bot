"""
Sleeper API adapter — free NFL/NBA/MLB player and roster data.

No API key required. Real-time roster, injury, and news data.
Particularly strong for NFL player projections and injury reports.
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.sleeper.app/v1"

# Sleeper sport identifiers
SPORT_MAP = {
    "americanfootball_nfl": "nfl",
    "basketball_nba":       "nba",
    "baseball_mlb":         "mlb",
}


def get_all_players(sport_key: str = "americanfootball_nfl") -> dict:
    """
    Returns all players for a sport as {player_id: player_dict}.
    Large payload (~5MB for NFL) — cache this, don't call every scan.
    """
    sport = SPORT_MAP.get(sport_key, "nfl")
    data = get_json(f"{_BASE}/players/{sport}")
    return data or {}


def get_trending_players(sport_key: str = "americanfootball_nfl",
                         trend_type: str = "add",
                         limit: int = 25) -> list[dict]:
    """
    trend_type: "add" (being added to rosters) | "drop" (being dropped)
    Rising adds often signal injury news or breakout performance.
    """
    sport = SPORT_MAP.get(sport_key, "nfl")
    data = get_json(f"{_BASE}/players/{sport}/trending/{trend_type}",
                    params={"lookback_hours": 24, "limit": limit})
    if not data:
        return []
    return [
        {
            "player_id": p.get("player_id"),
            "count":     p.get("count"),   # number of adds/drops
            "sport":     sport_key,
            "trend":     trend_type,
            "source":    "sleeper",
        }
        for p in data
    ]


def get_nfl_state() -> dict:
    """Returns current NFL season, week, and season type."""
    data = get_json(f"{_BASE}/state/nfl")
    return data or {}


def get_player_info(player_id: str, sport_key: str = "americanfootball_nfl") -> dict:
    """Look up a single player's profile data."""
    all_players = get_all_players(sport_key)
    p = all_players.get(player_id, {})
    if not p:
        return {}
    return {
        "player_id":  player_id,
        "name":       f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
        "position":   p.get("position", ""),
        "team":       p.get("team", ""),
        "status":     p.get("status", "Active"),   # "Active" | "Injured Reserve" | etc.
        "injury_status": p.get("injury_status", ""),  # "Out" | "Doubtful" | "Questionable"
        "injury_notes":  p.get("injury_notes", ""),
        "age":        p.get("age"),
        "years_exp":  p.get("years_exp"),
        "source":     "sleeper",
    }


def search_player(name: str, sport_key: str = "americanfootball_nfl") -> list[dict]:
    """Search for a player by name across the full Sleeper player list."""
    all_players = get_all_players(sport_key)
    name_lower = name.lower()
    matches = []
    for pid, p in all_players.items():
        full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".lower()
        if name_lower in full_name:
            matches.append({
                "player_id": pid,
                "name":      full_name.title(),
                "position":  p.get("position", ""),
                "team":      p.get("team", ""),
                "status":    p.get("status", ""),
                "injury_status": p.get("injury_status", ""),
                "source":    "sleeper",
            })
        if len(matches) >= 10:
            break
    return matches
