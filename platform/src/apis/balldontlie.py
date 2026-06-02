"""
Ball Don't Lie adapter — free NBA stats API (balldontlie.io).

Provides: players, teams, game logs, season averages, live scores.
No API key required for basic endpoints.
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.balldontlie.io/v1"


def search_player(name: str) -> list[dict]:
    data = get_json(f"{_BASE}/players", params={"search": name, "per_page": 10})
    if not data:
        return []
    return [
        {
            "id":       p.get("id"),
            "name":     f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "position": p.get("position", ""),
            "team":     p.get("team", {}).get("full_name", ""),
            "source":   "balldontlie",
        }
        for p in data.get("data", [])
    ]


def player_season_averages(player_id: int, season: int) -> dict:
    data = get_json(f"{_BASE}/season_averages", params={"player_ids[]": player_id, "season": season})
    if not data or not data.get("data"):
        return {}
    avg = data["data"][0]
    return {
        "player_id": player_id,
        "season":    season,
        "pts":       avg.get("pts"),
        "reb":       avg.get("reb"),
        "ast":       avg.get("ast"),
        "stl":       avg.get("stl"),
        "blk":       avg.get("blk"),
        "fg_pct":    avg.get("fg_pct"),
        "fg3_pct":   avg.get("fg3_pct"),
        "ft_pct":    avg.get("ft_pct"),
        "min":       avg.get("min"),
        "games_played": avg.get("games_played"),
        "source":    "balldontlie",
    }


def player_game_log(player_id: int, last_n: int = 10) -> list[dict]:
    data = get_json(f"{_BASE}/stats", params={
        "player_ids[]": player_id,
        "per_page": last_n,
        "sort_by": "game_date",
        "direction": "desc",
    })
    if not data:
        return []
    games = []
    for g in data.get("data", []):
        game = g.get("game", {})
        games.append({
            "date":     game.get("date", ""),
            "home":     game.get("home_team_score"),
            "away":     game.get("visitor_team_score"),
            "pts":      g.get("pts"),
            "reb":      g.get("reb"),
            "ast":      g.get("ast"),
            "min":      g.get("min"),
            "source":   "balldontlie",
        })
    return games


def get_games(date: str) -> list[dict]:
    """date format: YYYY-MM-DD"""
    data = get_json(f"{_BASE}/games", params={"dates[]": date, "per_page": 50})
    if not data:
        return []
    return [
        {
            "id":         g.get("id"),
            "date":       g.get("date"),
            "home_team":  g.get("home_team", {}).get("full_name", ""),
            "away_team":  g.get("visitor_team", {}).get("full_name", ""),
            "home_score": g.get("home_team_score"),
            "away_score": g.get("visitor_team_score"),
            "status":     g.get("status"),
            "source":     "balldontlie",
        }
        for g in data.get("data", [])
    ]


def get_teams() -> list[dict]:
    data = get_json(f"{_BASE}/teams", params={"per_page": 100})
    if not data:
        return []
    return [
        {
            "id":           t.get("id"),
            "name":         t.get("full_name"),
            "abbreviation": t.get("abbreviation"),
            "city":         t.get("city"),
            "conference":   t.get("conference"),
            "division":     t.get("division"),
            "source":       "balldontlie",
        }
        for t in data.get("data", [])
    ]
