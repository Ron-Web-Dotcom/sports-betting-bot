"""
SportsData.io adapter.

Provides real-time scores, standings, injuries, depth charts, player props,
DFS projections, and team/game stats across NFL, NBA, MLB, NHL, and Soccer.

Each sport requires its own subscription key set via env vars:
  SPORTSDATAIO_NFL_KEY    — NFL data
  SPORTSDATAIO_NBA_KEY    — NBA data
  SPORTSDATAIO_MLB_KEY    — MLB data
  SPORTSDATAIO_NHL_KEY    — NHL data
  SPORTSDATAIO_SOCCER_KEY — Soccer data

Auth: ?key={api_key} query parameter appended to every request.
Base URL pattern: https://api.sportsdata.io/v3/{sport}/{category}/json/{endpoint}

Endpoint reference: https://sportsdata.io/developers/api-documentation
"""
import logging
import os
from datetime import datetime

from src.apis.base import get_json

logger = logging.getLogger(__name__)

# ── Sport routing ──────────────────────────────────────────────────────────────

_SPORT_MAP: dict[str, tuple[str, str]] = {
    "americanfootball_nfl": ("nfl",    "SPORTSDATAIO_NFL_KEY"),
    "basketball_nba":       ("nba",    "SPORTSDATAIO_NBA_KEY"),
    "baseball_mlb":         ("mlb",    "SPORTSDATAIO_MLB_KEY"),
    "icehockey_nhl":        ("nhl",    "SPORTSDATAIO_NHL_KEY"),
}

_BASE = "https://api.sportsdata.io/v3"


def _resolve(sport_key: str) -> tuple[str, str] | tuple[None, None]:
    """Return (sdio_sport, api_key) or (None, None) if unconfigured."""
    if sport_key.startswith("soccer_") or sport_key == "soccer":
        key = os.getenv("SPORTSDATAIO_SOCCER_KEY", "").strip()
        return ("soccer", key) if key else (None, None)
    entry = _SPORT_MAP.get(sport_key)
    if not entry:
        return None, None
    sdio_sport, env_var = entry
    key = os.getenv(env_var, "").strip()
    return (sdio_sport, key) if key else (None, None)


def _get(sport_key: str, path: str, params: dict | None = None) -> list | dict | None:
    """GET from SportsData.io. Auth via ?key= query param."""
    sdio_sport, api_key = _resolve(sport_key)
    if not sdio_sport:
        return None
    url = f"{_BASE}/{sdio_sport}{path}"
    p = dict(params or {})
    p["key"] = api_key
    try:
        return get_json(url, params=p)
    except Exception as exc:
        logger.warning("sportsdataio GET %s failed: %s", path, exc)
        return None


# ── Season helper ──────────────────────────────────────────────────────────────

def _current_season(sdio_sport: str) -> str:
    """Return the current season string for use in endpoint URLs."""
    now = datetime.utcnow()
    year = now.year
    if sdio_sport == "nfl":
        # NFL season starts Sep; use current year if Aug+, else prev year
        return f"{year}REG" if now.month >= 8 else f"{year - 1}REG"
    if sdio_sport in ("nba", "nhl"):
        # Oct–Jun season; season labelled by end year
        return str(year) if now.month >= 10 else str(year - 1)
    if sdio_sport == "mlb":
        return str(year)
    return str(year)


# ── Public endpoints ───────────────────────────────────────────────────────────

def get_standings(sport_key: str) -> list[dict]:
    """
    Current standings for the given sport.

    NFL:  /scores/json/Standings/{season}
    NBA:  /scores/json/Standings/{season}
    MLB:  /scores/json/Standings/{season}
    NHL:  /scores/json/Standings/{season}
    """
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport:
        return []
    season = _current_season(sdio_sport)
    data = _get(sport_key, f"/scores/json/Standings/{season}")
    if not data:
        return []
    rows = data if isinstance(data, list) else []
    out = []
    for r in rows:
        out.append({
            "team":         r.get("Team") or r.get("Name", ""),
            "city":         r.get("City", ""),
            "wins":         r.get("Wins"),
            "losses":       r.get("Losses"),
            "ties":         r.get("Ties"),
            "win_pct":      r.get("Percentage") or r.get("WinPercentage"),
            "points_for":   r.get("PointsFor") or r.get("OffensiveYards"),
            "points_against": r.get("PointsAgainst") or r.get("DefensiveYards"),
            "streak":       r.get("StreakDescription"),
            "division":     r.get("Division"),
            "conference":   r.get("Conference"),
            "source":       "sportsdataio",
        })
    return out


def get_injuries(sport_key: str) -> list[dict]:
    """Current injury report, normalised across all sports."""
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport:
        return []
    paths = {
        "nfl":    "/scores/json/Injuries",
        "nba":    "/scores/json/PlayerInjuries",
        "mlb":    "/scores/json/PlayerInjuries",
        "nhl":    "/scores/json/PlayerInjuries",
        "soccer": "/scores/json/PlayerInjuries",
    }
    path = paths.get(sdio_sport)
    if not path:
        return []
    data = _get(sport_key, path)
    if not data:
        return []
    out = []
    for e in (data if isinstance(data, list) else []):
        out.append({
            "player_name": (
                e.get("Name") or e.get("PlayerName")
                or f"{e.get('FirstName','')} {e.get('LastName','')}".strip()
            ),
            "team":     e.get("Team") or e.get("TeamName", ""),
            "status":   e.get("Status") or e.get("InjuryStatus", ""),
            "position": e.get("Position", ""),
            "detail":   e.get("InjuryDescription") or e.get("Description") or e.get("Injury", ""),
            "source":   "sportsdataio",
        })
    return out


def get_team_stats(sport_key: str, team: str) -> dict:
    """Season stats for a team. Supported: NFL, NBA."""
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport or sdio_sport not in ("nfl", "nba"):
        return {}
    season = _current_season(sdio_sport)
    data = _get(sport_key, f"/stats/json/TeamSeasonStats/{season}")
    if not data:
        return {}
    team_lower = team.lower()
    for e in (data if isinstance(data, list) else []):
        name = (e.get("Team") or e.get("Name") or "").lower()
        city = (e.get("City") or "").lower()
        full = f"{city} {name}".strip()
        if team_lower in (name, full) or name in team_lower or team_lower in full:
            return {**e, "source": "sportsdataio"}
    return {}


def get_depth_chart(sport_key: str, team: str | None = None) -> list[dict]:
    """Depth chart entries. Supported: NFL, NBA."""
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport or sdio_sport not in ("nfl", "nba"):
        return []
    data = _get(sport_key, "/scores/json/DepthCharts")
    if not data:
        return []
    entries = data if isinstance(data, list) else []
    if team:
        t = team.lower()
        entries = [e for e in entries if (e.get("Team") or "").lower() == t]
    for e in entries:
        e["source"] = "sportsdataio"
    return entries


def get_schedule(sport_key: str) -> list[dict]:
    """Upcoming games for the current season."""
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport:
        return []
    season = _current_season(sdio_sport)
    data = _get(sport_key, f"/scores/json/Games/{season}")
    if not data:
        return []
    out = []
    for g in (data if isinstance(data, list) else []):
        out.append({
            "game_id":      g.get("GameID") or g.get("GameKey"),
            "date":         g.get("Date") or g.get("DateTime"),
            "home_team":    g.get("HomeTeam"),
            "away_team":    g.get("AwayTeam"),
            "home_score":   g.get("HomeScore"),
            "away_score":   g.get("AwayScore"),
            "status":       g.get("Status"),
            "stadium":      g.get("StadiumDetails", {}).get("Name") if isinstance(g.get("StadiumDetails"), dict) else None,
            "source":       "sportsdataio",
        })
    return out


def get_player_props(sport_key: str, game_id: str) -> list[dict]:
    """Player prop projections for a specific game."""
    sdio_sport, _ = _resolve(sport_key)
    if not sdio_sport:
        return []
    paths = {
        "nfl": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nba": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "mlb": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nhl": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
    }
    path = paths.get(sdio_sport)
    if not path:
        return []
    data = _get(sport_key, path)
    if not data:
        return []
    out = []
    for e in (data if isinstance(data, list) else []):
        player = (
            e.get("Name") or e.get("PlayerName")
            or f"{e.get('FirstName','')} {e.get('LastName','')}".strip()
        )
        for stat in ("PassingYards", "RushingYards", "ReceivingYards",
                     "Points", "Rebounds", "Assists", "Hits", "HomeRuns",
                     "Goals", "FantasyPoints"):
            val = e.get(stat)
            if val is not None:
                out.append({
                    "player":     player,
                    "team":       e.get("Team") or e.get("TeamName", ""),
                    "stat":       stat,
                    "projection": val,
                    "game_id":    game_id,
                    "source":     "sportsdataio",
                })
    return out


def enrich_game_context(sport_key: str, home_team: str, away_team: str) -> dict:
    """
    Aggregate standings, injuries, and team stats for both teams.
    Returns {"available": False} immediately if no key is configured.
    """
    sdio_sport, api_key = _resolve(sport_key)
    if not sdio_sport or not api_key:
        return {"available": False, "sport": sport_key, "source": "sportsdataio"}

    injuries   = get_injuries(sport_key)
    standings  = get_standings(sport_key)
    home_stats = get_team_stats(sport_key, home_team)
    away_stats = get_team_stats(sport_key, away_team)

    def _team_injuries(team: str) -> list:
        t = team.lower()
        return [i for i in injuries
                if t in (i.get("team") or "").lower()
                or (i.get("team") or "").lower() in t]

    return {
        "available":       True,
        "sport":           sport_key,
        "home_team":       home_team,
        "away_team":       away_team,
        "home_injuries":   _team_injuries(home_team),
        "away_injuries":   _team_injuries(away_team),
        "home_team_stats": home_stats,
        "away_team_stats": away_stats,
        "standings":       standings,
        "source":          "sportsdataio",
    }
