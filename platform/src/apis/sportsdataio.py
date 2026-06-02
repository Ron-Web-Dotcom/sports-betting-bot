"""
SportsData.io adapter.

Provides real-time scores, standings, injuries, depth charts, player props,
DFS projections, and team/game stats across NFL, NBA, MLB, NHL, and Soccer.

Single API key works across all sports — sport name is part of the URL path:
  https://api.sportsdata.io/v3/{sport}/scores/json/{endpoint}?key={key}

Set env var: SPORTSDATAIO_KEY

Endpoint reference: https://sportsdata.io/developers/api-documentation
"""
import logging
import os
from datetime import datetime

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.sportsdata.io/v3"

# Maps our internal sport_key → SportsData.io sport slug
_SPORT_SLUGS: dict[str, str] = {
    "americanfootball_nfl": "nfl",
    "basketball_nba":       "nba",
    "baseball_mlb":         "mlb",
    "icehockey_nhl":        "nhl",
    "soccer_epl":           "soccer",
    "soccer_usa_mls":       "soccer",
    "soccer_bundesliga":    "soccer",
    "soccer_uefa_champs_league": "soccer",
}


def _slug(sport_key: str) -> str | None:
    if sport_key.startswith("soccer_"):
        return "soccer"
    return _SPORT_SLUGS.get(sport_key)


def _key() -> str:
    return os.getenv("SPORTSDATAIO_KEY", "").strip()


def _get(sport_key: str, path: str, params: dict | None = None) -> list | dict | None:
    """
    GET from SportsData.io.
    URL: https://api.sportsdata.io/v3/{sport}{path}?key={key}
    """
    sport = _slug(sport_key)
    if not sport:
        return None
    api_key = _key()
    if not api_key:
        return None
    url = f"{_BASE}/{sport}{path}"
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
    sport = _slug(sport_key)
    if not sport:
        return []
    season = _current_season(sport)
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
    sport = _slug(sport_key)
    if not sport:
        return []
    paths = {
        "nfl":    "/scores/json/Injuries",
        "nba":    "/scores/json/PlayerInjuries",
        "mlb":    "/scores/json/PlayerInjuries",
        "nhl":    "/scores/json/PlayerInjuries",
        "soccer": "/scores/json/PlayerInjuries",
    }
    path = paths.get(sport)
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
    sport = _slug(sport_key)
    if not sport or sport not in ("nfl", "nba"):
        return {}
    season = _current_season(sport)
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
    sport = _slug(sport_key)
    if not sport or sport not in ("nfl", "nba"):
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
    sport = _slug(sport_key)
    if not sport:
        return []
    season = _current_season(sport)
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
    sport = _slug(sport_key)
    if not sport:
        return []
    paths = {
        "nfl": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nba": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "mlb": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
        "nhl": f"/projections/json/PlayerGameProjectionStatsByGameID/{game_id}",
    }
    path = paths.get(sport)
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
    sport, api_key = _slug(sport_key), _key()
    if not sport or not api_key:
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
