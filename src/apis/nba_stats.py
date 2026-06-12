"""
NBA / WNBA stats adapter — pulls team form from SofaScore.

SofaScore has full NBA/WNBA standings, form, and H2H. No key required.
Falls back gracefully if the team can't be found.
"""
import logging

logger = logging.getLogger(__name__)

_BASE = "https://api.sofascore.com/api/v1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sofascore.com/",
}

# SofaScore tournament IDs
_TOURNAMENT_ID = {
    "nba":  132,   # NBA
    "wnba": 536,   # WNBA
}

_season_cache: dict = {}


def _get_current_season_id(league: str) -> int | None:
    """Fetch the current/most recent season ID from SofaScore dynamically."""
    if league in _season_cache:
        return _season_cache[league]
    t_id = _TOURNAMENT_ID.get(league)
    if not t_id:
        return None
    data = _get(f"/tournament/{t_id}/seasons")
    seasons = (data or {}).get("seasons", []) if isinstance(data, dict) else []
    if not seasons:
        return None
    # First season in list is the most recent
    sid = seasons[0].get("id")
    if sid:
        _season_cache[league] = sid
        logger.debug("SofaScore season id for %s: %s", league, sid)
    return sid


def _get(path: str) -> dict | list | None:
    from src.apis.base import get_json
    return get_json(f"{_BASE}{path}", headers=_HEADERS)


def _search_team(team_name: str) -> dict | None:
    """Search SofaScore for a team by name, return the best match."""
    from urllib.parse import quote
    data = _get(f"/search/all/?q={quote(team_name)}")
    if not data:
        return None
    teams = data.get("teams", {}).get("hits", []) if isinstance(data, dict) else []
    name_l = team_name.lower()
    for hit in teams:
        entity = hit.get("entity", {})
        ename  = entity.get("name", "").lower()
        eshort = entity.get("shortName", "").lower()
        if name_l in ename or ename in name_l or name_l in eshort or eshort in name_l:
            return entity
    # fallback: return first result
    if teams:
        return teams[0].get("entity", {})
    return None


def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull NBA/WNBA team record and recent form from SofaScore.
    Returns: wins, losses, last_N_record, win_pct, streak, form_string
    """
    # Try SofaScore standings first
    t_id = _TOURNAMENT_ID.get(league)
    s_id = _get_current_season_id(league)

    if t_id and s_id:
        data = _get(f"/tournament/{t_id}/season/{s_id}/standings/total")
        rows = []
        if isinstance(data, dict):
            for block in (data.get("standings") or []):
                rows.extend(block.get("rows", []))
        name_l = team_name.lower()
        for row in rows:
            team_info = row.get("team", {})
            tname = team_info.get("name", "").lower()
            tshort = team_info.get("shortName", "").lower()
            if name_l in tname or tname in name_l or name_l in tshort or tshort in name_l:
                wins   = row.get("wins", 0) or 0
                losses = row.get("losses", 0) or 0
                win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0

                # Pull recent form via team events
                team_id = str(team_info.get("id", ""))
                form_str = ""
                streak = ""
                if team_id:
                    form_str, streak = _get_form_from_events(team_id, n)

                return {
                    "team":             team_info.get("name", team_name),
                    "wins":             wins,
                    "losses":           losses,
                    f"last_{n}_record": f"{wins}-{losses}",
                    "win_pct":          win_pct,
                    "streak":           streak,
                    "form":             form_str,
                    "source":           "sofascore_standings",
                }

    # Fallback: search for team directly
    entity = _search_team(team_name)
    if entity:
        team_id = str(entity.get("id", ""))
        if team_id:
            form_str, streak = _get_form_from_events(team_id, n)
            return {
                "team":             entity.get("name", team_name),
                "form":             form_str,
                "streak":           streak,
                "source":           "sofascore_events",
            }

    logger.debug("NBA stats: team '%s' not found in SofaScore", team_name)
    return {}


def _get_form_from_events(team_id: str, n: int = 10) -> tuple[str, str]:
    """
    Fetch last N completed events for a team and compute form string + streak.
    Returns (form_string, streak) e.g. ("WWLWW", "W2")
    """
    results = []
    for page in range(0, 3):  # pages 0,1,2 → up to ~15 events
        data = _get(f"/team/{team_id}/events/last/{page}")
        events = (data or {}).get("events", []) if isinstance(data, dict) else []
        if not events:
            break
        for ev in events:
            hs = (ev.get("homeScore") or {}).get("current")
            as_ = (ev.get("awayScore") or {}).get("current")
            if hs is None or as_ is None:
                continue
            home_id = str((ev.get("homeTeam") or {}).get("id", ""))
            is_home = home_id == team_id
            won = (hs > as_) if is_home else (as_ > hs)
            results.append("W" if won else "L")
            if len(results) >= n:
                break
        if len(results) >= n:
            break

    if not results:
        return "", ""

    form_str = "".join(results)

    # Compute streak from most recent
    streak_char = results[0]
    count = 0
    for r in results:
        if r == streak_char:
            count += 1
        else:
            break
    streak = f"{streak_char}{count}"

    return form_str, streak


def enrich_game_context(home_team: str, away_team: str, league: str = "nba") -> dict:
    """Pull NBA/WNBA context for a matchup from SofaScore."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home_form": (get_team_recent_form, (home_team, 10, league)),
        "away_form": (get_team_recent_form, (away_team, 10, league)),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        for future in _as_completed(futures, timeout=15):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                pass

    return {
        "nba_home_form":    results.get("home_form", {}),
        "nba_away_form":    results.get("away_form", {}),
        "nba_home_ratings": {},
        "nba_away_ratings": {},
    }
