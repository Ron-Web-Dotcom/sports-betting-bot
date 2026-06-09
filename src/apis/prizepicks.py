"""
PrizePicks adapter.

PrizePicks offers Over/Under lines for both players AND teams across
NFL, NBA, MLB, NHL, Soccer, Tennis, UFC, Golf, NCAAB, NCAAF, Esports, and more.

Player props:  Points, Rebounds, Assists, Passing Yards, Rushing Yards, etc.
Team props:    Total Points, Total Touchdowns, First Half Score,
               Team Rushing Yards, Team Passing Yards, etc.

Public API — no key required.
Base: https://api.prizepicks.com

Key endpoints:
  GET /projections?league_id={id}&per_page=250               — player + team props
  GET /projections?league_id={id}&is_team_prop=true          — team props only
  GET /leagues                                               — all leagues + IDs
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.prizepicks.com"
_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":          "https://app.prizepicks.com",
    "Referer":         "https://app.prizepicks.com/",
    "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "same-site",
}

# Maps our internal sport_key → PrizePicks league_id
# IDs sourced from their /leagues endpoint
# Run get_leagues() to verify/update IDs when new events are added
_LEAGUE_IDS: dict[str, int] = {
    "basketball_nba":                7,
    "americanfootball_nfl":          9,
    "baseball_mlb":                  2,
    "icehockey_nhl":                 6,
    "basketball_ncaab":              3,
    "americanfootball_ncaaf":        8,
    "soccer_epl":                    14,
    "soccer_usa_mls":                19,
    "soccer_fifa_world_cup":         37,   # FIFA World Cup 2026 — verify via /leagues
    "tennis_atp_french_open":        16,
    "mma_mixed_martial_arts":        10,
    "golf_us_open_winner":            12,   # Masters ended; US Open next
    "esports_lol":                   15,
}


def _get(path: str, params: dict | None = None) -> dict | list | None:
    try:
        return get_json(f"{_BASE}{path}", params=params, headers=_HEADERS)
    except Exception as e:
        logger.warning("PrizePicks GET %s failed: %s", path, e)
        return None


def get_leagues() -> list[dict]:
    """Return all active leagues with their IDs."""
    data = _get("/leagues")
    if not data:
        return []
    leagues = data if isinstance(data, list) else data.get("data", [])
    return [
        {
            "id":      l.get("id"),
            "name":    l.get("attributes", {}).get("name", ""),
            "sport":   l.get("attributes", {}).get("sport", ""),
            "active":  l.get("attributes", {}).get("active", False),
            "source":  "prizepicks",
        }
        for l in leagues
    ]


def _parse_projections(data: dict, sport_key: int, is_team: bool) -> list[dict]:
    """Parse PrizePicks JSON:API response into normalised prop dicts."""
    raw = data.get("data", []) if isinstance(data, dict) else []
    included = {
        item["id"]: item
        for item in (data.get("included", []) if isinstance(data, dict) else [])
    }
    logger.info("PrizePicks raw: %d projections, %d included (%s, team=%s)",
                len(raw), len(included), sport_key, is_team)

    out = []
    for proj in raw:
        attrs         = proj.get("attributes", {})
        relationships = proj.get("relationships", {})

        line = attrs.get("line_score")
        stat = attrs.get("stat_type", "")
        if line is None or not stat:
            continue

        if is_team:
            # Team prop — subject is the team, not a player
            team_rel  = relationships.get("new_player", {}).get("data", {})
            team_obj  = included.get(team_rel.get("id", ""), {})
            team_attrs = team_obj.get("attributes", {})
            subject   = attrs.get("name") or team_attrs.get("name", "Unknown Team")
            team      = subject
            player    = None
        else:
            # Player prop
            player_rel  = relationships.get("new_player", {}).get("data", {})
            player_obj  = included.get(player_rel.get("id", ""), {})
            player_attrs = player_obj.get("attributes", {})
            player      = attrs.get("name") or player_attrs.get("name", "Unknown")
            team        = player_attrs.get("team", "") or attrs.get("team", "")
            subject     = player

        out.append({
            "subject":     subject,
            "player":      player,       # None for team props
            "team":        team,
            "opponent":    attrs.get("opponent_name", ""),
            "stat":        stat,
            "line":        float(line),
            "game_time":   attrs.get("start_time", ""),
            "status":      attrs.get("status", ""),
            "is_team_prop": is_team,
            "sport_key":   sport_key,
            "source":      "prizepicks",
        })
    return out


def get_projections(sport_key: str) -> list[dict]:
    """
    Fetch ALL active projections for a sport — both player props and team props.
    Returns a combined normalised list. is_team_prop=True marks team props.
    """
    league_id = _LEAGUE_IDS.get(sport_key)
    if not league_id:
        logger.debug("PrizePicks: no league_id mapped for %s", sport_key)
        return []

    base_params = {"league_id": league_id, "per_page": 250, "single_stat": True}

    # Fetch player props and team props in parallel
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_player = pool.submit(_get, "/projections", base_params)
        f_team   = pool.submit(_get, "/projections", {**base_params, "is_team_prop": "true"})
        player_data = f_player.result()
        team_data   = f_team.result()

    if not player_data:
        logger.warning("PrizePicks: NULL response for %s player props (league_id=%s) — blocked or API down", sport_key, league_id)
    if not team_data:
        logger.warning("PrizePicks: NULL response for %s team props (league_id=%s)", sport_key, league_id)

    out = []
    if player_data:
        out.extend(_parse_projections(player_data, sport_key, is_team=False))
    if team_data:
        out.extend(_parse_projections(team_data, sport_key, is_team=True))

    player_count = sum(1 for p in out if not p["is_team_prop"])
    team_count   = sum(1 for p in out if p["is_team_prop"])
    logger.info("PrizePicks: %d player + %d team props for %s",
                player_count, team_count, sport_key)
    return out


def get_team_props(sport_key: str) -> list[dict]:
    """Fetch only team props for a sport."""
    return [p for p in get_projections(sport_key) if p["is_team_prop"]]


def get_player_props(sport_key: str) -> list[dict]:
    """Fetch only player props for a sport."""
    return [p for p in get_projections(sport_key) if not p["is_team_prop"]]


def get_all_projections() -> list[dict]:
    """Fetch all player + team projections for every mapped sport in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(get_projections, sport_key): sport_key
            for sport_key in _LEAGUE_IDS
        }
        for future in as_completed(futures, timeout=30):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.warning("PrizePicks fetch failed for %s: %s", futures[future], e)
    return results


def find_prop(sport_key: str, name: str, stat: str) -> dict | None:
    """Find a player or team prop by name and stat type."""
    name_lower = name.lower()
    stat_lower = stat.lower()
    for proj in get_projections(sport_key):
        if (name_lower in (proj["subject"] or "").lower()
                and stat_lower in proj["stat"].lower()):
            return proj
    return None
