"""
SportsData.io adapter.

SportsData.io provides real-time scores, player props, DFS projections,
depth charts, injury reports, team season stats, and game logs across
NFL, NBA, MLB, NHL, and Soccer.

Each sport uses its own API key and base URL.  Keys are read from
environment variables at call time so they can be injected without
restarting the process.

Auth is via the Ocp-Apim-Subscription-Key request header.

Sport-specific base URLs:
  NFL:    https://api.sportsdata.io/v3/nfl
  NBA:    https://api.sportsdata.io/v3/nba
  MLB:    https://api.sportsdata.io/v3/mlb
  NHL:    https://api.sportsdata.io/v3/nhl
  Soccer: https://api.sportsdata.io/v3/soccer
"""
import logging
import os

from src.apis.base import get_json

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sport routing
# ---------------------------------------------------------------------------

# Maps our internal sport_key prefix/exact value → (sdio_sport, env_var)
_SPORT_MAP: dict[str, tuple[str, str]] = {
    "americanfootball_nfl": ("nfl",    "SPORTSDATAIO_NFL_KEY"),
    "basketball_nba":       ("nba",    "SPORTSDATAIO_NBA_KEY"),
    "baseball_mlb":         ("mlb",    "SPORTSDATAIO_MLB_KEY"),
    "icehockey_nhl":        ("nhl",    "SPORTSDATAIO_NHL_KEY"),
}

_BASE_URLS: dict[str, str] = {
    "nfl":    "https://api.sportsdata.io/v3/nfl",
    "nba":    "https://api.sportsdata.io/v3/nba",
    "mlb":    "https://api.sportsdata.io/v3/mlb",
    "nhl":    "https://api.sportsdata.io/v3/nhl",
    "soccer": "https://api.sportsdata.io/v3/soccer",
}

# Injury endpoints vary by sport
_INJURY_PATHS: dict[str, str] = {
    "nfl":    "/scores/json/Injuries",
    "nba":    "/scores/json/PlayerInjuries",
    "mlb":    "/scores/json/PlayerInjuries",
    "nhl":    "/scores/json/PlayerInjuries",
    "soccer": "/scores/json/PlayerInjuries",
}


def _resolve_sport(sport_key: str) -> str | None:
    """
    Translate our internal sport_key to a SportsData.io sport slug.
    Any sport_key starting with 'soccer_' maps to 'soccer'.
    """
    if sport_key.startswith("soccer_") or sport_key == "soccer":
        return "soccer"
    entry = _SPORT_MAP.get(sport_key)
    return entry[0] if entry else None


def _env_var_for_sport(sdio_sport: str) -> str:
    """Return the environment variable name for the given sdio sport slug."""
    mapping = {
        "nfl":    "SPORTSDATAIO_NFL_KEY",
        "nba":    "SPORTSDATAIO_NBA_KEY",
        "mlb":    "SPORTSDATAIO_MLB_KEY",
        "nhl":    "SPORTSDATAIO_NHL_KEY",
        "soccer": "SPORTSDATAIO_SOCCER_KEY",
    }
    return mapping.get(sdio_sport, "")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_key(sport_key: str) -> tuple[str, str] | tuple[None, None]:
    """
    Resolve sport_key to (base_url, api_key).
    Returns (None, None) if the sport is unsupported or the key is not set.
    """
    sdio_sport = _resolve_sport(sport_key)
    if not sdio_sport:
        logger.debug("sportsdataio: unsupported sport_key '%s'", sport_key)
        return None, None

    env_var = _env_var_for_sport(sdio_sport)
    api_key = os.environ.get(env_var, "").strip()
    if not api_key:
        logger.debug("sportsdataio: env var %s not set for sport '%s'", env_var, sport_key)
        return None, None

    base_url = _BASE_URLS[sdio_sport]
    return base_url, api_key


def _get(sport_key: str, path: str, params: dict | None = None) -> dict | list | None:
    """
    Make an authenticated GET request to the SportsData.io endpoint for the
    given sport.  Returns parsed JSON or None on failure.
    """
    base_url, api_key = _get_key(sport_key)
    if not base_url:
        return None

    url = f"{base_url}{path}"
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    return get_json(url, params=params, headers=headers)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_injuries(sport_key: str) -> list[dict]:
    """
    Return the current injury report for the given sport.

    Normalised fields per entry:
      player_name, team, status, position, detail
    """
    sdio_sport = _resolve_sport(sport_key)
    if not sdio_sport:
        return []

    _, api_key = _get_key(sport_key)
    if not api_key:
        logger.warning("sportsdataio: no API key configured for '%s', skipping injuries", sport_key)
        return []

    path = _INJURY_PATHS.get(sdio_sport)
    if not path:
        logger.warning("sportsdataio: no injury endpoint for sport '%s'", sdio_sport)
        return []

    data = _get(sport_key, path)
    if data is None:
        logger.warning("sportsdataio: failed to fetch injuries for '%s'", sport_key)
        return []

    raw = data if isinstance(data, list) else []
    result = []
    for entry in raw:
        result.append({
            "player_name": (
                entry.get("Name")
                or entry.get("PlayerName")
                or f"{entry.get('FirstName', '')} {entry.get('LastName', '')}".strip()
            ),
            "team":     entry.get("Team") or entry.get("TeamName", ""),
            "status":   entry.get("Status") or entry.get("InjuryStatus", ""),
            "position": entry.get("Position", ""),
            "detail":   (
                entry.get("InjuryDescription")
                or entry.get("Description")
                or entry.get("Injury", "")
            ),
            "source":   "sportsdataio",
        })
    return result


def get_team_stats(sport_key: str, team: str) -> dict:
    """
    Return current season statistics for the given team abbreviation.

    Supported sports: NFL, NBA.  Returns an empty dict for others.
    """
    sdio_sport = _resolve_sport(sport_key)
    if sdio_sport not in ("nfl", "nba"):
        logger.warning(
            "sportsdataio: get_team_stats not supported for sport '%s'", sport_key
        )
        return {}

    _, api_key = _get_key(sport_key)
    if not api_key:
        logger.warning("sportsdataio: no API key configured for '%s', skipping team stats", sport_key)
        return {}

    data = _get(sport_key, "/stats/json/TeamSeasonStats/current")
    if data is None:
        logger.warning("sportsdataio: failed to fetch team stats for '%s'", sport_key)
        return {}

    teams = data if isinstance(data, list) else []
    team_lower = team.lower()
    for entry in teams:
        name  = (entry.get("Team") or entry.get("TeamName") or "").lower()
        city  = (entry.get("City") or "").lower()
        full  = f"{city} {name}".strip()
        if team_lower in (name, full) or name in team_lower or team_lower in full:
            return {**entry, "source": "sportsdataio"}

    logger.warning("sportsdataio: team '%s' not found in season stats for '%s'", team, sport_key)
    return {}


def get_depth_chart(sport_key: str, team: str | None = None) -> list[dict]:
    """
    Return depth chart entries.  Supported sports: NFL, NBA.

    If `team` is provided the list is filtered to that team abbreviation.
    """
    sdio_sport = _resolve_sport(sport_key)
    if sdio_sport not in ("nfl", "nba"):
        logger.warning(
            "sportsdataio: get_depth_chart not supported for sport '%s'", sport_key
        )
        return []

    _, api_key = _get_key(sport_key)
    if not api_key:
        logger.warning("sportsdataio: no API key configured for '%s', skipping depth chart", sport_key)
        return []

    data = _get(sport_key, "/scores/json/DepthCharts")
    if data is None:
        logger.warning("sportsdataio: failed to fetch depth charts for '%s'", sport_key)
        return []

    entries = data if isinstance(data, list) else []
    if team:
        team_lower = team.lower()
        entries = [
            e for e in entries
            if (e.get("Team") or e.get("TeamName") or "").lower() == team_lower
        ]

    for entry in entries:
        entry["source"] = "sportsdataio"
    return entries


def get_player_props(sport_key: str, game_id: str) -> list[dict]:
    """
    Return player prop projections for a specific game.

    Each entry contains: player, stat, projection, line.
    """
    _, api_key = _get_key(sport_key)
    if not api_key:
        logger.warning("sportsdataio: no API key configured for '%s', skipping player props", sport_key)
        return []

    sdio_sport = _resolve_sport(sport_key)

    # Endpoint varies by sport; use the DFS projections endpoint where available
    paths_by_sport: dict[str, str] = {
        "nfl":    f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nba":    f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "mlb":    f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nhl":    f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "soccer": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
    }
    path = paths_by_sport.get(sdio_sport or "")
    if not path:
        logger.warning("sportsdataio: no player props endpoint for sport '%s'", sport_key)
        return []

    data = _get(sport_key, path)
    if data is None:
        logger.warning(
            "sportsdataio: failed to fetch player props for game '%s' (%s)", game_id, sport_key
        )
        return []

    raw = data if isinstance(data, list) else []
    result = []
    for entry in raw:
        player_name = (
            entry.get("Name")
            or entry.get("PlayerName")
            or f"{entry.get('FirstName', '')} {entry.get('LastName', '')}".strip()
        )
        # Emit one row per projected stat that also has a fantasy-line analog
        for stat_key, line_key in [
            ("FantasyPoints",         "FantasyPointsFanDuel"),
            ("PassingYards",          None),
            ("RushingYards",          None),
            ("ReceivingYards",        None),
            ("Points",                None),
            ("Rebounds",              None),
            ("Assists",               None),
            ("HomeRuns",              None),
            ("RBIs",                  None),
            ("Goals",                 None),
        ]:
            projection = entry.get(stat_key)
            if projection is None:
                continue
            result.append({
                "player":     player_name,
                "team":       entry.get("Team") or entry.get("TeamName", ""),
                "stat":       stat_key,
                "projection": projection,
                "line":       entry.get(line_key) if line_key else None,
                "game_id":    game_id,
                "source":     "sportsdataio",
            })
    return result


def get_dfs_slates(sport_key: str) -> list[dict]:
    """
    Return DFS slate information and per-player projections for the current day.
    """
    _, api_key = _get_key(sport_key)
    if not api_key:
        logger.warning("sportsdataio: no API key configured for '%s', skipping DFS slates", sport_key)
        return []

    sdio_sport = _resolve_sport(sport_key)
    paths_by_sport: dict[str, str] = {
        "nfl":    "/projections/json/DfsSlatesByDate/{date}",
        "nba":    "/projections/json/DfsSlatesByDate/{date}",
        "mlb":    "/projections/json/DfsSlatesByDate/{date}",
        "nhl":    "/projections/json/DfsSlatesByDate/{date}",
    }
    path_template = paths_by_sport.get(sdio_sport or "")
    if not path_template:
        logger.warning("sportsdataio: no DFS slates endpoint for sport '%s'", sport_key)
        return []

    from datetime import datetime
    today = datetime.utcnow().strftime("%Y-%b-%d").upper()  # e.g. 2026-JUN-02
    path = path_template.replace("{date}", today)

    data = _get(sport_key, path)
    if data is None:
        logger.warning("sportsdataio: failed to fetch DFS slates for '%s'", sport_key)
        return []

    slates = data if isinstance(data, list) else []
    for slate in slates:
        slate["source"] = "sportsdataio"
    return slates


def enrich_game_context(
    sport_key: str,
    home_team: str,
    away_team: str,
) -> dict:
    """
    Aggregate injuries and season stats for both teams into a single context dict.

    Returns {"available": False} immediately when no API key is configured for
    the sport, so callers can skip further processing cheaply.

    On partial failure (e.g. stats fetch fails) the function still returns what
    it managed to collect, with the "available" flag set to True.
    """
    _, api_key = _get_key(sport_key)
    if not api_key:
        return {"available": False, "sport": sport_key, "source": "sportsdataio"}

    injuries = get_injuries(sport_key)

    def _filter_injuries(team: str) -> list[dict]:
        t = team.lower()
        return [
            i for i in injuries
            if t in (i.get("team") or "").lower()
            or (i.get("team") or "").lower() in t
        ]

    home_stats = get_team_stats(sport_key, home_team)
    away_stats = get_team_stats(sport_key, away_team)

    return {
        "available":        True,
        "sport":            sport_key,
        "home_team":        home_team,
        "away_team":        away_team,
        "home_injuries":    _filter_injuries(home_team),
        "away_injuries":    _filter_injuries(away_team),
        "home_team_stats":  home_stats,
        "away_team_stats":  away_stats,
        "source":           "sportsdataio",
    }
