"""
TheSportsDB adapter.

TheSportsDB (https://www.thesportsdb.com/) provides team info, last/next events,
player data, league tables, and event details across many sports.

Free tier: base URL uses key "3" (public free key).
Paid tier: set SPORTSDB_API_KEY to your real key.

Env var: SPORTSDB_API_KEY  (defaults to "3" if not set)

Key endpoints used:
  GET searchteams.php?t={team_name}              search teams by name
  GET eventslast.php?id={team_id}                last events for a team
  GET eventsnext.php?id={team_id}                next events for a team
  GET lookupevent.php?id={event_id}              full event detail
  GET searchplayers.php?p={player_name}          search players by name
  GET lookuptable.php?l={league_id}&s=current    league standings
"""
import logging
import os

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("SPORTSDB_API_KEY", "")
_BASE = "https://www.thesportsdb.com/api/v1/json"

# Maps our internal sport_key → TheSportsDB league ID
LEAGUE_MAP: dict[str, int] = {
    "americanfootball_nfl":      4391,
    "basketball_nba":            4387,
    "baseball_mlb":              4424,
    "icehockey_nhl":             4380,
    "soccer_epl":                4328,
    "soccer_usa_mls":            4346,
    "soccer_bundesliga":         4331,
    "soccer_uefa_champs_league": 4480,
}


# ── Internal HTTP helper ───────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict | list | None:
    """Build URL as {_BASE}/{key}/{path} and return parsed JSON or None."""
    if not _API_KEY:
        return None
    url = f"{_BASE}/{_API_KEY}/{path}"
    try:
        return get_json(url, params=params)
    except Exception as exc:
        logger.warning("TheSportsDB GET %s failed: %s", path, exc)
        return None


# ── Team search ───────────────────────────────────────────────────────────────

def search_team(team_name: str) -> dict:
    """
    Search for a team by name.

    Returns a dict with: id, name, sport, country, formed_year,
    description, badge_url — or an empty dict on failure / no match.
    """
    try:
        data = _get("searchteams.php", params={"t": team_name})
        if not data:
            return {}
        teams = data if isinstance(data, list) else data.get("teams") or []
        if not teams:
            return {}
        t = teams[0]
        return {
            "id":          str(t.get("idTeam", "")),
            "name":        t.get("strTeam", ""),
            "sport":       t.get("strSport", ""),
            "country":     t.get("strCountry", ""),
            "formed_year": t.get("intFormedYear", ""),
            "description": t.get("strDescriptionEN", ""),
            "badge_url":   t.get("strTeamBadge", ""),
            "source":      "thesportsdb",
        }
    except Exception as exc:
        logger.warning("search_team(%r) failed: %s", team_name, exc)
        return {}


# ── Team events ───────────────────────────────────────────────────────────────

def _normalise_event(e: dict) -> dict:
    """Normalise a raw TheSportsDB event dict to a consistent shape."""
    home_score = e.get("intHomeScore")
    away_score = e.get("intAwayScore")
    return {
        "id":         str(e.get("idEvent", "")),
        "name":       e.get("strEvent", ""),
        "home_team":  e.get("strHomeTeam", ""),
        "away_team":  e.get("strAwayTeam", ""),
        "home_score": int(home_score) if home_score not in (None, "") else None,
        "away_score": int(away_score) if away_score not in (None, "") else None,
        "date":       e.get("dateEvent", ""),
        "time":       e.get("strTime", ""),
        "league":     e.get("strLeague", ""),
        "season":     e.get("strSeason", ""),
        "venue":      e.get("strVenue", ""),
        "status":     e.get("strStatus", ""),
        "source":     "thesportsdb",
    }


def get_team_last_events(team_id: str, n: int = 5) -> list[dict]:
    """
    Return the most recent completed events for a team.

    team_id: TheSportsDB numeric team ID (string or int accepted).
    n: maximum number of results to return.
    """
    try:
        data = _get("eventslast.php", params={"id": str(team_id)})
        if not data:
            return []
        events = data if isinstance(data, list) else data.get("results") or []
        return [_normalise_event(e) for e in events[:n]]
    except Exception as exc:
        logger.warning("get_team_last_events(%r) failed: %s", team_id, exc)
        return []


def get_team_next_events(team_id: str, n: int = 5) -> list[dict]:
    """
    Return the next scheduled (upcoming) events for a team.

    team_id: TheSportsDB numeric team ID (string or int accepted).
    n: maximum number of results to return.
    """
    try:
        data = _get("eventsnext.php", params={"id": str(team_id)})
        if not data:
            return []
        events = data if isinstance(data, list) else data.get("events") or []
        return [_normalise_event(e) for e in events[:n]]
    except Exception as exc:
        logger.warning("get_team_next_events(%r) failed: %s", team_id, exc)
        return []


# ── Event detail ──────────────────────────────────────────────────────────────

def get_event_stats(event_id: str) -> dict:
    """
    Return full event detail for a given TheSportsDB event ID.

    Returns a normalised dict on success, or an empty dict on failure.
    """
    try:
        data = _get("lookupevent.php", params={"id": str(event_id)})
        if not data:
            return {}
        events = data if isinstance(data, list) else data.get("events") or []
        if not events:
            return {}
        return _normalise_event(events[0])
    except Exception as exc:
        logger.warning("get_event_stats(%r) failed: %s", event_id, exc)
        return {}


# ── Head-to-head ──────────────────────────────────────────────────────────────

def get_head_to_head(team1_name: str, team2_name: str) -> dict:
    """
    Construct a head-to-head summary between two teams by name.

    Searches for each team, fetches their last events, then finds
    matches where both teams appear.  Returns a dict with keys:
      team1, team2, meetings (list of matching events), source.
    """
    try:
        t1 = search_team(team1_name)
        t2 = search_team(team2_name)

        if not t1 or not t2:
            logger.warning(
                "get_head_to_head: could not find one or both teams (%r, %r)",
                team1_name, team2_name,
            )
            return {
                "team1":    team1_name,
                "team2":    team2_name,
                "meetings": [],
                "source":   "thesportsdb",
            }

        t1_id = t1["id"]
        t2_id = t2["id"]
        t1_name_lower = t1["name"].lower()
        t2_name_lower = t2["name"].lower()

        # Pull recent events for both teams and intersect
        t1_events = get_team_last_events(t1_id, n=25)
        t2_event_ids = {e["id"] for e in get_team_last_events(t2_id, n=25)}

        meetings = []
        for ev in t1_events:
            # Match by shared event ID or by both team names appearing
            home_lower = ev.get("home_team", "").lower()
            away_lower = ev.get("away_team", "").lower()
            involves_t2 = (
                ev["id"] in t2_event_ids
                or t2_name_lower in home_lower
                or t2_name_lower in away_lower
            )
            if involves_t2:
                meetings.append(ev)

        return {
            "team1":    t1["name"],
            "team2":    t2["name"],
            "meetings": meetings,
            "source":   "thesportsdb",
        }
    except Exception as exc:
        logger.warning(
            "get_head_to_head(%r, %r) failed: %s", team1_name, team2_name, exc
        )
        return {
            "team1":    team1_name,
            "team2":    team2_name,
            "meetings": [],
            "source":   "thesportsdb",
        }


# ── League table ──────────────────────────────────────────────────────────────

def get_league_table(league_id: int | str) -> list[dict]:
    """
    Return the current standings for a league.

    league_id: TheSportsDB numeric league ID.
    Returns a list of standing-row dicts, or an empty list on failure.
    """
    try:
        data = _get("lookuptable.php", params={"l": str(league_id), "s": "current"})
        if not data:
            return []
        rows = data if isinstance(data, list) else data.get("table") or []
        result = []
        for row in rows:
            result.append({
                "position":      row.get("intRank"),
                "team":          row.get("strTeam", ""),
                "team_id":       str(row.get("idTeam", "")),
                "played":        row.get("intPlayed"),
                "wins":          row.get("intWin"),
                "draws":         row.get("intDraw"),
                "losses":        row.get("intLoss"),
                "goals_for":     row.get("intGoalsFor"),
                "goals_against": row.get("intGoalsAgainst"),
                "goal_diff":     row.get("intGoalDifference"),
                "points":        row.get("intPoints"),
                "source":        "thesportsdb",
            })
        return result
    except Exception as exc:
        logger.warning("get_league_table(%r) failed: %s", league_id, exc)
        return []


# ── Player search ─────────────────────────────────────────────────────────────

def get_player(player_name: str) -> dict:
    """
    Search for a player by name and return the first match.

    Returns a dict with: id, name, team, nationality, position,
    description, thumb_url — or an empty dict on failure / no match.
    """
    try:
        data = _get("searchplayers.php", params={"p": player_name})
        if not data:
            return {}
        players = data if isinstance(data, list) else data.get("player") or []
        if not players:
            return {}
        p = players[0]
        return {
            "id":          str(p.get("idPlayer", "")),
            "name":        p.get("strPlayer", ""),
            "team":        p.get("strTeam", ""),
            "nationality": p.get("strNationality", ""),
            "position":    p.get("strPosition", ""),
            "description": p.get("strDescriptionEN", ""),
            "thumb_url":   p.get("strThumb", ""),
            "birth_date":  p.get("dateBorn", ""),
            "height":      p.get("strHeight", ""),
            "weight":      p.get("strWeight", ""),
            "source":      "thesportsdb",
        }
    except Exception as exc:
        logger.warning("get_player(%r) failed: %s", player_name, exc)
        return {}


# ── Unified enrichment ────────────────────────────────────────────────────────

def enrich_game_context(sport_key: str, home_team: str, away_team: str) -> dict:
    """
    Unified enrichment for a matchup using TheSportsDB data.

    Looks up both teams, pulls their recent/upcoming events, constructs a
    head-to-head summary, and fetches the relevant league table.

    Returns a dict with an "available" key (True when at least one team was
    found) plus team info, recent results, upcoming fixtures, h2h, and
    standings.
    """
    try:
        home_info = search_team(home_team)
        away_info = search_team(away_team)

        if not home_info and not away_info:
            return {
                "available":   False,
                "sport":       sport_key,
                "home_team":   home_team,
                "away_team":   away_team,
                "source":      "thesportsdb",
            }

        home_id = home_info.get("id")
        away_id = away_info.get("id")

        home_last   = get_team_last_events(home_id)   if home_id else []
        home_next   = get_team_next_events(home_id)   if home_id else []
        away_last   = get_team_last_events(away_id)   if away_id else []
        away_next   = get_team_next_events(away_id)   if away_id else []
        h2h         = get_head_to_head(home_team, away_team)

        league_id   = LEAGUE_MAP.get(sport_key)
        standings   = get_league_table(league_id) if league_id else []

        return {
            "available":        True,
            "sport":            sport_key,
            "home_team_info":   home_info,
            "away_team_info":   away_info,
            "home_last_events": home_last,
            "home_next_events": home_next,
            "away_last_events": away_last,
            "away_next_events": away_next,
            "head_to_head":     h2h,
            "standings":        standings,
            "source":           "thesportsdb",
        }
    except Exception as exc:
        logger.warning(
            "enrich_game_context(%r, %r, %r) failed: %s",
            sport_key, home_team, away_team, exc,
        )
        return {
            "available": False,
            "sport":     sport_key,
            "home_team": home_team,
            "away_team": away_team,
            "source":    "thesportsdb",
        }
