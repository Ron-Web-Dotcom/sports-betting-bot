"""
Sleeper API adapter — free NFL/NBA/MLB player projections + roster data.

No API key required. Public endpoints:
  /projections/{sport}/{season}/{week}  — weekly stat projections (prop lines)
  /v1/players/{sport}                   — full player roster + injury status
  /v1/players/{sport}/trending/add|drop — trending adds/drops (injury signals)
  /v1/state/{sport}                     — current season/week metadata

Projections are used as prop-line equivalents (same underlying data
that Sleeper's own Player Picks pick'em product surfaces).
"""
import logging
from src.apis.base import get_json
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

_BASE    = "https://api.sleeper.app/v1"
_BASE_V2 = "https://api.sleeper.app"   # projections live here (no /v1/)

# Sleeper projections are NFL-only (NBA/MLB endpoints return empty lists)
# NBA/MLB injury data still works via get_injured_players()
SPORT_MAP = {
    "americanfootball_nfl": "nfl",
}

# Sports supported for roster/injury data (broader than projections)
INJURY_SPORT_MAP = {
    "americanfootball_nfl": "nfl",
    "basketball_nba":       "nba",
    "baseball_mlb":         "mlb",
}

# Stat key → human display name + typical line context
_STAT_DISPLAY: dict[str, str] = {
    # NFL
    "pass_yd":   "Passing Yards",
    "pass_td":   "Passing TDs",
    "pass_att":  "Pass Attempts",
    "rush_yd":   "Rushing Yards",
    "rush_att":  "Rush Attempts",
    "rush_td":   "Rushing TDs",
    "rec":       "Receptions",
    "rec_yd":    "Receiving Yards",
    "rec_td":    "Receiving TDs",
    "rec_tgt":   "Targets",
    # NBA
    "pts":       "Points",
    "reb":       "Rebounds",
    "ast":       "Assists",
    "stl":       "Steals",
    "blk":       "Blocks",
    "tov":       "Turnovers",
    "fg3m":      "3-Pointers Made",
    # MLB
    "hit":       "Hits",
    "hr":        "Home Runs",
    "rbi":       "RBIs",
    "r":         "Runs",
    "sb":        "Stolen Bases",
    "k":         "Strikeouts",
    "ip":        "Innings Pitched",
    "er":        "Earned Runs",
}

# Minimum projection value to surface as a prop line (filters noise)
_MIN_THRESHOLDS: dict[str, float] = {
    "pass_yd": 50, "rush_yd": 10, "rec_yd": 10,
    "pts": 5,      "reb": 2,      "ast": 1,
}


def _current_season_week(sport: str) -> tuple[int, str, int]:
    """
    Return (season_year, season_type, week) for the given sport.
    Uses Sleeper state API — falls back to current year + week 1 if unavailable.
    """
    state = get_json(f"{_BASE}/state/{sport}") or {}
    season      = int(state.get("season", et_naive().year))
    season_type = state.get("season_type", "regular")  # regular | post | pre | off
    week        = int(state.get("week", 1))
    # Use display_week if available (more accurate for current games)
    display_week = state.get("display_week")
    if display_week:
        week = int(display_week)
    return season, season_type, week


def get_projections(sport_key: str, season: int | None = None, week: int | None = None) -> list[dict]:
    """
    Fetch weekly player stat projections from Sleeper and return them as
    prop-line dicts compatible with the prop_engine / scan_player_props pipeline.

    Each dict matches the shape expected by score_props():
      source, sport_key, subject, stat, line, game_time, team, opponent
    """
    sport = SPORT_MAP.get(sport_key)
    if not sport:
        return []

    if season is None or week is None:
        season, season_type, week = _current_season_week(sport)
    else:
        season_type = "regular"

    url = f"{_BASE_V2}/projections/{sport}/{season}/{week}"
    params = {"season_type": season_type}
    # add position filters for NFL to keep payload manageable
    if sport == "nfl":
        params["position[]"] = ["QB", "RB", "WR", "TE", "K"]

    raw = get_json(url, params=params)
    # Response must be a dict of {player_id: projection} — skip if list or empty
    if not raw or not isinstance(raw, dict):
        return []

    # Fetch player roster once to resolve names + teams
    all_players = get_all_players(sport_key)

    props: list[dict] = []
    for player_id, proj in raw.items():
        p = all_players.get(player_id, {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue
        team = p.get("team", "")
        stats: dict = proj.get("stats", proj)  # response varies by version

        for stat_key, stat_label in _STAT_DISPLAY.items():
            value = stats.get(stat_key)
            if value is None:
                continue
            value = float(value)
            # skip if below meaningful threshold
            if value < _MIN_THRESHOLDS.get(stat_key, 0.5):
                continue

            props.append({
                "source":    "sleeper",
                "sport_key": sport_key,
                "subject":   name,
                "stat":      stat_label,
                "line":      round(value, 1),
                "team":      team,
                "opponent":  "",   # Sleeper projections don't embed opponent
                "game_time": "",
            })

    logger.info("Sleeper: %d prop lines for %s week %s", len(props), sport_key, week)
    return props


def get_all_projections() -> list[dict]:
    """Fetch projections for all supported sports and return combined prop list."""
    out: list[dict] = []
    for sport_key in SPORT_MAP:
        out.extend(get_projections(sport_key))
    return out


def get_player_props(sport_key: str) -> list[dict]:
    """Alias used by prop_engine / odds_worker."""
    return get_projections(sport_key)


def get_all_props() -> list[dict]:
    """Alias used by morning brief / change detection."""
    return get_all_projections()


def get_all_players(sport_key: str = "americanfootball_nfl") -> dict:
    """
    Returns all players for a sport as {player_id: player_dict}.
    Large payload (~5MB for NFL) — cache this, don't call every scan.
    """
    sport = INJURY_SPORT_MAP.get(sport_key, "nfl")
    data = get_json(f"{_BASE}/players/{sport}")
    return data or {}


def get_trending_players(sport_key: str = "americanfootball_nfl",
                         trend_type: str = "add",
                         limit: int = 25) -> list[dict]:
    """
    trend_type: "add" (being added to rosters) | "drop" (being dropped)
    Rising adds often signal injury news or breakout performance.
    """
    sport = SPORT_MAP.get(sport_key, "nfl")
    data = get_json(f"{_BASE}/players/{sport}/trending/{trend_type}",
                    params={"lookback_hours": 24, "limit": limit})
    if not data:
        return []
    return [
        {
            "player_id": p.get("player_id"),
            "count":     p.get("count"),   # number of adds/drops
            "sport":     sport_key,
            "trend":     trend_type,
            "source":    "sleeper",
        }
        for p in data
    ]


def get_sport_state(sport_key: str) -> dict:
    """Returns current season/week metadata for any supported sport."""
    sport = SPORT_MAP.get(sport_key, "nfl")
    return get_json(f"{_BASE}/state/{sport}") or {}


def get_nfl_state() -> dict:
    """Returns current NFL season, week, and season type."""
    return get_sport_state("americanfootball_nfl")


def get_injured_players(sport_key: str) -> list[dict]:
    """
    Return all players with a non-active injury status for a sport.
    Used to enrich ESPN injury data and catch updates ESPN misses.

    Sleeper injury_status values: "Out", "Doubtful", "Questionable",
    "Injured_Reserve", "PUP", "NFI", "Suspended", "GTD", "Day-To-Day"
    """
    all_players = get_all_players(sport_key)
    injured = []
    for pid, p in all_players.items():
        inj_status = (p.get("injury_status") or "").strip()
        status     = (p.get("status") or "Active").strip()

        # Skip fully active / practice squad / undrafted etc.
        if not inj_status and status == "Active":
            continue
        if status in {"Inactive", "Practice Squad", ""}:
            continue

        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue

        injured.append({
            "player_name":   name,
            "player_id":     pid,
            "team":          p.get("team", ""),
            "position":      p.get("position", ""),
            "status":        status,
            "injury_status": inj_status,
            "injury_notes":  p.get("injury_notes", ""),
            "sport":         sport_key,
            "source":        "sleeper",
        })

    logger.info("Sleeper injured players: %d for %s", len(injured), sport_key)
    return injured


def get_all_injured_players() -> list[dict]:
    """Return injured players across all Sleeper-supported sports (NFL/NBA/MLB)."""
    out: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(get_injured_players, sk): sk for sk in INJURY_SPORT_MAP}
        for fut in as_completed(futures, timeout=20):
            try:
                out.extend(fut.result())
            except Exception as e:
                logger.warning("Sleeper injured fetch failed [%s]: %s", futures[fut], e)
    return out


def get_trending_drops(sport_key: str = "americanfootball_nfl", limit: int = 25) -> list[dict]:
    """
    Players being dropped from rosters — strong signal for injury or benching.
    Complements get_injured_players() by catching news before official reports.
    """
    return get_trending_players(sport_key, trend_type="drop", limit=limit)


def get_player_info(player_id: str, sport_key: str = "americanfootball_nfl") -> dict:
    """Look up a single player's profile data."""
    all_players = get_all_players(sport_key)
    p = all_players.get(player_id, {})
    if not p:
        return {}
    return {
        "player_id":  player_id,
        "name":       f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
        "position":   p.get("position", ""),
        "team":       p.get("team", ""),
        "status":     p.get("status", "Active"),   # "Active" | "Injured Reserve" | etc.
        "injury_status": p.get("injury_status", ""),  # "Out" | "Doubtful" | "Questionable"
        "injury_notes":  p.get("injury_notes", ""),
        "age":        p.get("age"),
        "years_exp":  p.get("years_exp"),
        "source":     "sleeper",
    }


def search_player(name: str, sport_key: str = "americanfootball_nfl") -> list[dict]:
    """Search for a player by name across the full Sleeper player list."""
    all_players = get_all_players(sport_key)
    name_lower = name.lower()
    matches = []
    for pid, p in all_players.items():
        full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".lower()
        if name_lower in full_name:
            matches.append({
                "player_id": pid,
                "name":      full_name.title(),
                "position":  p.get("position", ""),
                "team":      p.get("team", ""),
                "status":    p.get("status", ""),
                "injury_status": p.get("injury_status", ""),
                "source":    "sleeper",
            })
        if len(matches) >= 10:
            break
    return matches
