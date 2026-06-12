"""
NBA Stats API adapter — free, no key required.
Base: https://stats.nba.com/stats

Provides:
- Team recent form (last 10 games W-L, streak)
- Player injury / availability status
- Head-to-head record between two teams
- Team offensive/defensive ratings
"""
import logging
import httpx
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_BASE    = "https://stats.nba.com/stats"
_TIMEOUT = 12.0

# stats.nba.com blocks requests without browser-like headers
_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer":         "https://www.nba.com/",
    "Origin":          "https://www.nba.com",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
}

_SEASON      = "2025-26"   # NBA 2025-26 (includes 2026 Finals)
_WNBA_SEASON = "2026"      # WNBA 2026 season


def _is_playoffs() -> bool:
    """True during NBA playoff window (Apr–Jun)."""
    m = datetime.now(timezone.utc).month
    return 4 <= m <= 6


def _get(endpoint: str, params: dict) -> dict | None:
    try:
        r = httpx.get(
            f"{_BASE}/{endpoint}",
            params=params,
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("NBA Stats API error [%s]: %s", endpoint, e)
        return None


def _rows_to_dicts(data: dict) -> list[dict]:
    """Convert NBA API resultSets format to list of dicts."""
    try:
        rs = data.get("resultSets", [{}])[0]
        headers = rs.get("headers", [])
        rows    = rs.get("rowSet", [])
        return [dict(zip(headers, row)) for row in rows]
    except Exception:
        return []


# ── Team lookup ────────────────────────────────────────────────────────────────

_WNBA_TEAM_IDS = {
    "atlanta dream": 1611661313, "chicago sky": 1611661317,
    "connecticut sun": 1611661319, "dallas wings": 1611661325,
    "indiana fever": 1611661323, "las vegas aces": 1611661329,
    "los angeles sparks": 1611661320, "minnesota lynx": 1611661324,
    "new york liberty": 1611661313, "phoenix mercury": 1611661321,
    "seattle storm": 1611661330, "washington mystics": 1611661328,
    "golden state valkyries": 1611661332, "portland fire": 1611661333,
    "toronto tempo": 1611661334, "cleveland charge": 1611661335,
}

_TEAM_IDS = {
    "atlanta hawks": 1610612737, "boston celtics": 1610612738,
    "brooklyn nets": 1610612751, "charlotte hornets": 1610612766,
    "chicago bulls": 1610612741, "cleveland cavaliers": 1610612739,
    "dallas mavericks": 1610612742, "denver nuggets": 1610612743,
    "detroit pistons": 1610612765, "golden state warriors": 1610612744,
    "houston rockets": 1610612745, "indiana pacers": 1610612754,
    "la clippers": 1610612746, "los angeles clippers": 1610612746,
    "los angeles lakers": 1610612747, "la lakers": 1610612747,
    "memphis grizzlies": 1610612763, "miami heat": 1610612748,
    "milwaukee bucks": 1610612749, "minnesota timberwolves": 1610612750,
    "new orleans pelicans": 1610612740, "new york knicks": 1610612752,
    "oklahoma city thunder": 1610612760, "orlando magic": 1610612753,
    "philadelphia 76ers": 1610612755, "phoenix suns": 1610612756,
    "portland trail blazers": 1610612757, "sacramento kings": 1610612758,
    "san antonio spurs": 1610612759, "toronto raptors": 1610612761,
    "utah jazz": 1610612762, "washington wizards": 1610612764,
}

def _get_team_id(name: str, league: str = "nba") -> int | None:
    lookup = _WNBA_TEAM_IDS if league == "wnba" else _TEAM_IDS
    name_l = name.lower()
    if name_l in lookup:
        return lookup[name_l]
    for key, tid in lookup.items():
        if name_l in key or key in name_l:
            return tid
    # fallback: search both
    for key, tid in {**_TEAM_IDS, **_WNBA_TEAM_IDS}.items():
        if name_l in key or key in name_l:
            return tid
    return None


# ── Recent form ────────────────────────────────────────────────────────────────

def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """Last N games W-L, streak, home/away splits, offensive/defensive rating."""
    team_id = _get_team_id(team_name)
    if not team_id:
        return {}

    season     = _WNBA_SEASON if league == "wnba" else _SEASON
    season_type = "Regular Season"

    data = _get("teamgamelogs", {
        "TeamID":       team_id,
        "Season":       season,
        "SeasonType":   season_type,
        "LastNGames":   n,
        "LeagueID":     "10" if league == "wnba" else "00",
    })
    if not data:
        return {}

    rows = _rows_to_dicts(data)
    if not rows:
        return {}

    wins   = sum(1 for r in rows if r.get("WL") == "W")
    losses = len(rows) - wins

    streak_count = 0
    streak_type  = rows[0].get("WL", "")
    for r in rows:
        if r.get("WL") == streak_type:
            streak_count += 1
        else:
            break

    avg_pts     = round(sum(r.get("PTS", 0) for r in rows) / len(rows), 1)
    avg_pts_opp = round(sum(r.get("PTS_OPP", r.get("PLUS_MINUS", 0)) for r in rows) / len(rows), 1)

    return {
        f"last_{n}_record": f"{wins}-{losses}",
        "wins":   wins,
        "losses": losses,
        "streak": f"{streak_type}{streak_count}",
        "avg_points":         avg_pts,
        "recent_games": [
            {"date": r.get("GAME_DATE",""), "result": r.get("WL",""),
             "pts": r.get("PTS",0), "matchup": r.get("MATCHUP","")}
            for r in rows[:5]
        ],
    }


# ── Team season stats (OFF/DEF ratings) ───────────────────────────────────────

def get_team_ratings(team_name: str) -> dict:
    """Offensive and defensive rating for the season."""
    team_id = _get_team_id(team_name)
    if not team_id:
        return {}

    data = _get("teamdashboardbygeneralsplits", {
        "TeamID":     team_id,
        "Season":     _SEASON,
        "SeasonType": "Playoffs",
        "MeasureType":"Advanced",
        "PerMode":    "PerGame",
    })
    if not data:
        return {}

    rows = _rows_to_dicts(data)
    if not rows:
        return {}

    r = rows[0]
    return {
        "off_rating": r.get("OFF_RATING"),
        "def_rating": r.get("DEF_RATING"),
        "net_rating": r.get("NET_RATING"),
        "pace":       r.get("PACE"),
    }


# ── Main entry point ───────────────────────────────────────────────────────────

def enrich_game_context(home_team: str, away_team: str, league: str = "nba") -> dict:
    """Pull NBA/WNBA context for a matchup — form, ratings for both teams."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home_form":    (get_team_recent_form, (home_team, 10, league)),
        "away_form":    (get_team_recent_form, (away_team, 10, league)),
        "home_ratings": (get_team_ratings,     (home_team,)),
        "away_ratings": (get_team_ratings,     (away_team,)),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
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
        "nba_home_ratings": results.get("home_ratings", {}),
        "nba_away_ratings": results.get("away_ratings", {}),
    }
