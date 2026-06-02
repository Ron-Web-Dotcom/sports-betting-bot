"""
API-Sports adapter (https://api-sports.io/).

API-Sports provides comprehensive sports data across American football, basketball,
baseball, hockey, soccer, and rugby, including team and player statistics, injury
reports, league standings, head-to-head history, and pre-game betting odds.

Authentication: every request must include the header ``x-apisports-key`` set to
the value of the ``API_SPORTS_KEY`` environment variable.  When the variable is not
set all public functions return empty dicts / lists so the rest of the pipeline
continues without this optional enrichment source.

Base URL pattern: ``https://v1.<sport-slug>.api-sports.io``
  e.g. https://v1.american-football.api-sports.io
       https://v1.basketball.api-sports.io
"""
import logging
import os
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_KEY_ENV = "API_SPORTS_KEY"

# Valid sport slugs accepted by the API-Sports platform.
_VALID_SLUGS = {
    "american-football",
    "basketball",
    "baseball",
    "hockey",
    "soccer",
    "rugby",
}

# Maps our internal sport_key → (api-sports slug, default league_id)
SPORT_MAP: dict[str, tuple[str, int]] = {
    "americanfootball_nfl":          ("american-football", 1),
    "basketball_nba":                ("basketball",        12),
    "baseball_mlb":                  ("baseball",          1),
    "icehockey_nhl":                 ("hockey",            57),
    "soccer_epl":                    ("soccer",            39),
    "soccer_usa_mls":                ("soccer",           253),
    "soccer_bundesliga":             ("soccer",            78),
    "soccer_uefa_champs_league":     ("soccer",             2),
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _api_key() -> str | None:
    return os.getenv(_KEY_ENV)


def _get(
    sport_slug: str,
    path: str,
    params: dict | None = None,
) -> dict | list | None:
    """
    Issue a GET request to the API-Sports endpoint for *sport_slug*.

    Injects the ``x-apisports-key`` auth header automatically.  Returns the
    parsed JSON body (dict or list) or ``None`` on any failure.
    """
    key = _api_key()
    if not key:
        logger.warning("API_SPORTS_KEY not set — skipping API-Sports call to /%s%s", sport_slug, path)
        return None

    if sport_slug not in _VALID_SLUGS:
        logger.warning("Unknown API-Sports sport slug: %s", sport_slug)
        return None

    url = f"https://v1.{sport_slug}.api-sports.io{path}"
    headers = {"x-apisports-key": key}
    try:
        return get_json(url, params=params, headers=headers)
    except Exception as exc:
        logger.warning("API-Sports GET %s failed: %s", url, exc)
        return None


def _sport_info(sport_key: str) -> tuple[str, int] | tuple[None, None]:
    """Return (slug, league_id) for *sport_key*, or (None, None) if unmapped."""
    entry = SPORT_MAP.get(sport_key)
    if not entry:
        logger.warning("API-Sports: unmapped sport_key '%s'", sport_key)
        return None, None
    return entry


# ── Public API ────────────────────────────────────────────────────────────────

def get_team_stats(sport_key: str, team_name: str) -> dict:
    """
    Return season statistics for the team identified by *team_name*.

    Searches the league associated with *sport_key* for a team whose name
    contains *team_name* (case-insensitive), then fetches its season stats.

    Returned dict keys (present when data is available):
        team, wins, losses, draws, points_per_game, points_allowed_per_game,
        games_played, win_pct, source
    """
    slug, league_id = _sport_info(sport_key)
    if slug is None:
        return {}

    try:
        # Step 1: resolve team id from name
        search_data = _get(slug, "/teams", params={"search": team_name})
        if not search_data:
            return {}

        teams = search_data if isinstance(search_data, list) else search_data.get("response", [])
        team_id = None
        team_display = team_name
        for entry in teams:
            t = entry.get("team", entry)
            name = t.get("name", "")
            if team_name.lower() in name.lower() or name.lower() in team_name.lower():
                team_id = t.get("id")
                team_display = name
                break

        if team_id is None:
            return {}

        # Step 2: fetch statistics for that team in the default league
        stats_data = _get(slug, "/teams/statistics", params={
            "team":   team_id,
            "league": league_id,
        })
        if not stats_data:
            return {}

        resp = stats_data if isinstance(stats_data, dict) else (
            stats_data[0] if stats_data else {}
        )
        resp = resp.get("response", resp)

        games = resp.get("games", {})
        played     = games.get("played", {})
        wins_d     = resp.get("wins",   {}).get("total", {})
        losses_d   = resp.get("loses",  {}).get("total", {})  # API-Sports spells it "loses"
        draws_d    = resp.get("draws",  {}).get("total", {})
        goals_for  = resp.get("goals",  {}).get("for",     {}).get("average", {})
        goals_ag   = resp.get("goals",  {}).get("against", {}).get("average", {})

        total_played = (played.get("total") or played) if isinstance(played, int) else played.get("total")
        wins         = (wins_d.get("total")   or wins_d)   if isinstance(wins_d,   dict) else wins_d
        losses       = (losses_d.get("total") or losses_d) if isinstance(losses_d, dict) else losses_d
        draws        = (draws_d.get("total")  or draws_d)  if isinstance(draws_d,  dict) else draws_d
        ppg          = goals_for.get("total")  if isinstance(goals_for, dict) else goals_for
        papg         = goals_ag.get("total")   if isinstance(goals_ag,  dict) else goals_ag

        win_pct = None
        if total_played and isinstance(wins, (int, float)) and total_played > 0:
            win_pct = round(wins / total_played, 3)

        return {
            "team":                   team_display,
            "games_played":           total_played,
            "wins":                   wins,
            "losses":                 losses,
            "draws":                  draws,
            "points_per_game":        ppg,
            "points_allowed_per_game": papg,
            "win_pct":                win_pct,
            "source":                 "api-sports",
        }
    except Exception as exc:
        logger.warning("get_team_stats(%s, %s) failed: %s", sport_key, team_name, exc)
        return {}


def get_injuries(sport_key: str) -> list[dict]:
    """
    Return the current injury list for the default league of *sport_key*.

    Each item in the returned list contains:
        player_name, team, status, detail, source
    """
    slug, league_id = _sport_info(sport_key)
    if slug is None:
        return []

    try:
        data = _get(slug, "/injuries", params={"league": league_id})
        if not data:
            return []

        items = data if isinstance(data, list) else data.get("response", [])
        result = []
        for entry in items:
            player = entry.get("player", {})
            team   = entry.get("team",   {})
            result.append({
                "player_name": player.get("name", ""),
                "player_id":   str(player.get("id", "")),
                "team":        team.get("name", ""),
                "status":      entry.get("type", ""),
                "detail":      entry.get("reason", ""),
                "source":      "api-sports",
            })
        return result
    except Exception as exc:
        logger.warning("get_injuries(%s) failed: %s", sport_key, exc)
        return []


def get_standings(sport_key: str, league_id: int | None = None) -> list[dict]:
    """
    Return the current standings for *sport_key*.

    *league_id* defaults to the value in SPORT_MAP for the given sport_key.

    Each item in the returned list contains:
        position, team, team_id, played, wins, draws, losses,
        goals_for, goals_against, points, form, source
    """
    slug, default_league_id = _sport_info(sport_key)
    if slug is None:
        return []

    lid = league_id if league_id is not None else default_league_id

    try:
        data = _get(slug, "/standings", params={"league": lid})
        if not data:
            return []

        response = data if isinstance(data, list) else data.get("response", [])

        # The standings endpoint returns a nested structure:
        # response[0].league.standings[0] → list of team rows
        rows = []
        if response and isinstance(response[0], dict):
            league_obj = response[0].get("league", response[0])
            standings_groups = league_obj.get("standings", [])
            if standings_groups and isinstance(standings_groups[0], list):
                rows = standings_groups[0]
            elif isinstance(standings_groups, list):
                rows = standings_groups

        result = []
        for row in rows:
            team    = row.get("team", {})
            all_rec = row.get("all", {})
            goals   = all_rec.get("goals", {})
            result.append({
                "position":      row.get("rank"),
                "team":          team.get("name", ""),
                "team_id":       str(team.get("id", "")),
                "played":        all_rec.get("played"),
                "wins":          all_rec.get("win"),
                "draws":         all_rec.get("draw"),
                "losses":        all_rec.get("lose"),
                "goals_for":     goals.get("for"),
                "goals_against": goals.get("against"),
                "points":        row.get("points"),
                "form":          row.get("form", ""),
                "source":        "api-sports",
            })
        return result
    except Exception as exc:
        logger.warning("get_standings(%s) failed: %s", sport_key, exc)
        return []


def get_head_to_head(sport_key: str, team1: str, team2: str) -> dict:
    """
    Return the last 10 head-to-head games between *team1* and *team2*.

    The returned dict contains:
        games       – list of individual game dicts
        record      – {"team1_wins": n, "team2_wins": n, "draws": n}
        team1       – resolved team name
        team2       – resolved team name
        source
    """
    slug, league_id = _sport_info(sport_key)
    if slug is None:
        return {}

    try:
        # Resolve team ids
        def _resolve_id(name: str) -> tuple[int | None, str]:
            d = _get(slug, "/teams", params={"search": name})
            if not d:
                return None, name
            items = d if isinstance(d, list) else d.get("response", [])
            for entry in items:
                t = entry.get("team", entry)
                t_name = t.get("name", "")
                if name.lower() in t_name.lower() or t_name.lower() in name.lower():
                    return t.get("id"), t_name
            return None, name

        id1, name1 = _resolve_id(team1)
        id2, name2 = _resolve_id(team2)
        if id1 is None or id2 is None:
            return {"available": False, "source": "api-sports"}

        data = _get(slug, "/games/h2h", params={"h2h": f"{id1}-{id2}", "last": 10})
        if not data:
            return {}

        games_raw = data if isinstance(data, list) else data.get("response", [])

        games = []
        t1_wins = t2_wins = draws = 0
        for g in games_raw:
            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            scores = g.get("scores", g.get("goals", {}))
            hs = scores.get("home") if isinstance(scores, dict) else None
            as_ = scores.get("away") if isinstance(scores, dict) else None

            # Determine winner from winner flag if available
            winner_flag = home.get("winner")  # True/False/None
            if winner_flag is True:
                winner = home.get("name", "")
            elif winner_flag is False:
                winner = away.get("name", "")
            else:
                winner = "draw"

            if winner == name1 or (id1 and home.get("id") == id1 and winner_flag is True):
                t1_wins += 1
            elif winner == name2 or (id2 and away.get("id") == id2 and winner_flag is False):
                t2_wins += 1
            else:
                draws += 1

            games.append({
                "home_team":  home.get("name", ""),
                "away_team":  away.get("name", ""),
                "home_score": hs,
                "away_score": as_,
                "winner":     winner,
                "date":       g.get("date", ""),
                "source":     "api-sports",
            })

        return {
            "team1":  name1,
            "team2":  name2,
            "games":  games,
            "record": {
                "team1_wins": t1_wins,
                "team2_wins": t2_wins,
                "draws":      draws,
            },
            "source": "api-sports",
        }
    except Exception as exc:
        logger.warning("get_head_to_head(%s, %s, %s) failed: %s", sport_key, team1, team2, exc)
        return {}


def get_game_odds(sport_key: str, game_id: int | str) -> dict:
    """
    Return pre-game odds for *game_id* from the API-Sports odds endpoint.

    The returned dict contains:
        game_id, bookmakers (list of {name, bets}), source
    """
    slug, league_id = _sport_info(sport_key)
    if slug is None:
        return {}

    try:
        data = _get(slug, "/odds", params={"game": game_id})
        if not data:
            return {}

        response = data if isinstance(data, list) else data.get("response", [])
        if not response:
            return {}

        entry       = response[0] if isinstance(response[0], dict) else {}
        bookmakers  = entry.get("bookmakers", [])

        parsed_books = []
        for bk in bookmakers:
            bets = []
            for bet in bk.get("bets", []):
                bets.append({
                    "name":   bet.get("name", ""),
                    "values": bet.get("values", []),
                })
            parsed_books.append({
                "name": bk.get("name", ""),
                "id":   bk.get("id"),
                "bets": bets,
            })

        return {
            "game_id":    game_id,
            "bookmakers": parsed_books,
            "source":     "api-sports",
        }
    except Exception as exc:
        logger.warning("get_game_odds(%s, %s) failed: %s", sport_key, game_id, exc)
        return {}


def enrich_game_context(
    sport_key: str,
    home_team: str,
    away_team: str,
) -> dict:
    """
    Fetch and combine multiple API-Sports data points for a single match-up.

    Calls get_team_stats, get_injuries, get_standings, and get_head_to_head in
    sequence and bundles the results.  Returns a dict with ``"available": True``
    when at least partial data could be retrieved, otherwise ``"available": False``.

    Returned dict keys:
        available, sport, home_team_stats, away_team_stats, injuries,
        standings, head_to_head, source
    """
    slug, _league_id = _sport_info(sport_key)
    if slug is None:
        return {"available": False, "sport": sport_key, "source": "api-sports"}

    if not _api_key():
        return {"available": False, "sport": sport_key, "source": "api-sports"}

    home_stats  = get_team_stats(sport_key, home_team)
    away_stats  = get_team_stats(sport_key, away_team)
    injuries    = get_injuries(sport_key)
    standings   = get_standings(sport_key)
    h2h         = get_head_to_head(sport_key, home_team, away_team)

    available = any([home_stats, away_stats, injuries, standings, h2h])

    return {
        "available":        available,
        "sport":            sport_key,
        "home_team_stats":  home_stats,
        "away_team_stats":  away_stats,
        "injuries":         injuries,
        "standings":        standings,
        "head_to_head":     h2h,
        "source":           "api-sports",
    }
