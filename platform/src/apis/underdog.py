"""
Underdog Fantasy adapter.

Underdog is a player props / DFS platform covering NFL, NBA, MLB, NHL,
NCAAF, NCAAB, Golf, Tennis, Soccer, UFC, and NASCAR.

Public API — no key required.
Base: https://api.underdogfantasy.com/beta

Key endpoints:
  GET /v1/over_under_lines   — all active Over/Under props
  GET /v1/appearances        — player-game appearance data
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.underdogfantasy.com/beta"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://underdogfantasy.com/",
}

# Maps our internal sport_key → Underdog sport_id string
_SPORT_MAP: dict[str, str] = {
    "basketball_nba":                 "NBA",
    "americanfootball_nfl":           "NFL",
    "baseball_mlb":                   "MLB",
    "icehockey_nhl":                  "NHL",
    "basketball_ncaab":               "NCAAB",
    "americanfootball_ncaaf":         "NCAAF",
    "soccer_epl":                     "SOCCER",
    "soccer_usa_mls":                 "SOCCER",
    "mma_mixed_martial_arts":         "MMA",
    "golf_masters_tournament_winner": "GOLF",
    "tennis_atp_french_open":         "TENNIS",
}


def _get(path: str) -> dict | list | None:
    try:
        return get_json(f"{_BASE}{path}", headers=_HEADERS)
    except Exception as e:
        logger.warning("Underdog GET %s failed: %s", path, e)
        return None


def get_over_under_lines(sport_key: str | None = None) -> list[dict]:
    """
    Fetch all active Over/Under player prop lines.
    Optionally filtered by sport_key.
    """
    data = _get("/v1/over_under_lines")
    if not data:
        return []

    raw = data if isinstance(data, list) else data.get("over_under_lines", [])
    appearances = {
        a["id"]: a
        for a in (data.get("appearances", []) if isinstance(data, dict) else [])
    }
    players = {
        p["id"]: p
        for p in (data.get("players", []) if isinstance(data, dict) else [])
    }

    target_sport = _SPORT_MAP.get(sport_key, "") if sport_key else ""

    out = []
    for line in raw:
        appearance_id = line.get("over_under", {}).get("appearance_stat", {}).get("appearance_id")
        app = appearances.get(str(appearance_id), {})
        player_id = app.get("player_id")
        player = players.get(str(player_id), {})

        sport = app.get("sport_id", "")
        if target_sport and sport.upper() != target_sport.upper():
            continue

        stat = line.get("over_under", {}).get("appearance_stat", {}).get("display_stat", "")
        line_score = line.get("stat_value")
        if line_score is None:
            continue

        out.append({
            "player":    f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
            "team":      app.get("team_abbreviation", ""),
            "opponent":  app.get("opponent_abbreviation", ""),
            "stat":      stat,
            "line":      float(line_score),
            "game_time": app.get("match_time", ""),
            "sport":     sport,
            "sport_key": sport_key or "",
            "source":    "underdog",
        })

    logger.info("Underdog: %d lines for %s", len(out), sport_key or "all")
    return out


def get_all_lines() -> list[dict]:
    """Fetch all active lines across all sports."""
    return get_over_under_lines(sport_key=None)
