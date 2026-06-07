"""
Underdog Fantasy adapter.

Underdog is a player props / DFS platform covering NFL, NBA, MLB, NHL,
NCAAF, NCAAB, Golf, Tennis, Soccer, UFC, and NASCAR.

Public API — no key required.
Base: https://api.underdogfantasy.com

Confirmed endpoint (v5):
  GET /beta/v5/over_under_lines   — all active pick'em props (three-array response)

Response shape:
  { "players": [...], "appearances": [...], "over_under_lines": [...] }
  Join: over_under_lines[].over_under.appearance_stat.appearance_id
        → appearances[].id → appearances[].player_id → players[].id
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.underdogfantasy.com"
_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.google.com/",
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
    "soccer_fifa_world_cup":          "SOCCER",
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
    Fetch all active pick'em Over/Under lines from Underdog Fantasy.
    Uses confirmed v5 endpoint. Optionally filter by sport_key.

    Join pattern:
      over_under_lines[].over_under.appearance_stat.appearance_id
      → appearances[id] → appearances[player_id] → players[id]
    """
    data = _get("/beta/v5/over_under_lines")
    if not data or not isinstance(data, dict):
        return []

    players = {p["id"]: p for p in data.get("players", [])}
    appearances = {a["id"]: a for a in data.get("appearances", [])}
    lines = data.get("over_under_lines", [])

    target_sport = _SPORT_MAP.get(sport_key, "") if sport_key else ""

    out = []
    for line in lines:
        ou = line.get("over_under", {})
        app_stat = ou.get("appearance_stat", {})

        appearance_id = app_stat.get("appearance_id")
        app = appearances.get(str(appearance_id), {})
        player = players.get(str(app.get("player_id", "")), {})

        sport = app.get("sport_id", "")
        if target_sport and sport.upper() != target_sport.upper():
            continue

        # stat_value lives on over_under, not on the line root
        line_val = ou.get("stat_value")
        if line_val is None:
            line_val = line.get("stat_value")
        if line_val is None:
            continue

        stat = app_stat.get("display_stat") or app_stat.get("stat", "")

        out.append({
            "player":    f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
            "team":      app.get("team_abbreviation", ""),
            "opponent":  app.get("opponent_abbreviation", ""),
            "stat":      stat,
            "line":      float(line_val),
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


def get_player_props(sport_key: str) -> list[dict]:
    """Alias used by prop_engine — returns player lines for a given sport."""
    return get_over_under_lines(sport_key=sport_key)


def get_all_props() -> list[dict]:
    """Return all props across all sports (used by morning brief / change detection)."""
    return get_over_under_lines(sport_key=None)
