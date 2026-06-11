"""
Sportradar API adapter.

Provides real H2H records, recent form (last N games), team stats,
player profiles, and injury feeds for NBA, NFL, MLB, NHL, Soccer, and MMA.

Auth: x-api-key header (never use query param — modern endpoints only).
Base: https://api.sportradar.com
Trial: 1,000 queries / 30 days, 1 QPS — production tiers are higher.

All public methods return plain dicts/lists and degrade gracefully to {} / [].
"""
import logging
import time
import threading

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.sportradar.com"

# Thread-safe 1 QPS gate (trial limit; production is higher but we keep it safe)
_last_call_ts: float = 0.0
_rate_lock = threading.Lock()
_MIN_INTERVAL = 1.05  # slightly over 1 s to be safe


# ── Internal HTTP helper ───────────────────────────────────────────────────────

def _get(path: str, access: str = "trial") -> dict | list | None:
    """Rate-limited GET against the Sportradar REST API."""
    from src.core.config import SPORTRADAR_API_KEY
    if not SPORTRADAR_API_KEY:
        return None

    global _last_call_ts
    with _rate_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.monotonic()

    url = f"{_BASE}{path}"
    try:
        r = httpx.get(
            url,
            headers={"accept": "application/json", "x-api-key": SPORTRADAR_API_KEY},
            timeout=15.0,
        )
        if r.status_code == 429:
            logger.warning("Sportradar rate limited — backing off 2s")
            time.sleep(2)
            return None
        if r.status_code in (404, 422):
            logger.debug("Sportradar 404/422 for %s (off-season or unknown ID)", path)
            return None
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Sportradar HTTP %s: %s", e.response.status_code, path)
        return None
    except Exception as e:
        logger.warning("Sportradar fetch failed [%s]: %s", path, e)
        return None


# ── Sport routing helpers ──────────────────────────────────────────────────────

# Maps our Odds API sport_key → (sportradar_sport, version, access_qualifier)
_SPORT_ROUTES: dict[str, tuple[str, str, str]] = {
    "basketball_nba":            ("nba",  "v8", "trial"),
    "basketball_wnba":           ("wnba", "v1", "trial"),
    "americanfootball_nfl":      ("nfl",  "v7", "official/trial"),
    "americanfootball_ncaaf":    ("ncaaf","v7", "trial"),
    "baseball_mlb":              ("mlb",  "v8", "trial"),
    "icehockey_nhl":             ("nhl",  "v7", "trial"),
    "mma_mixed_martial_arts":    ("mma",  "v2", "trial"),
}

_SOCCER_SPORTS = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_usa_mls",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_mexico_ligamx", "soccer_argentina_primera_division",
    "soccer_brazil_campeonato", "soccer_turkey_super_league", "soccer_spl",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_fifa_world_cup",
    "soccer_conmebol_copa_america", "soccer_conmebol_copa_libertadores",
    "soccer_africa_cup_of_nations", "soccer_usa_nwsl",
    "soccer_fifa_womens_world_cup", "soccer_uefa_womens_champs_league",
}


def _sport_base(sport_key: str) -> str | None:
    """Return the API path prefix for a sport, e.g. '/nba/trial/v8/en'."""
    if sport_key in _SOCCER_SPORTS:
        return "/soccer/trial/v4/en"
    route = _SPORT_ROUTES.get(sport_key)
    if not route:
        return None
    sport, version, access = route
    return f"/{sport}/{access}/{version}/en"


# ── Sportradar ID lookup (cached in memory) ───────────────────────────────────

_team_id_cache: dict[str, str] = {}     # "sport_key:team_name" → sportradar_id
_player_id_cache: dict[str, str] = {}   # "sport_key:player_name" → sportradar_id
_id_cache_lock = threading.Lock()


def _find_team_id(sport_key: str, team_name: str) -> str | None:
    """
    Look up a Sportradar team/competitor ID by partial name match.
    Caches results to avoid repeated hierarchy calls.
    """
    cache_key = f"{sport_key}:{team_name.lower()}"
    with _id_cache_lock:
        if cache_key in _team_id_cache:
            return _team_id_cache[cache_key]

    base = _sport_base(sport_key)
    if not base:
        return None

    # Soccer uses 'competitors' hierarchy; others use 'league/hierarchy'
    if sport_key in _SOCCER_SPORTS:
        # Soccer: search via competitor search endpoint
        data = _get(f"{base}/competitors.json?name={team_name[:20]}")
        competitors = (data or {}).get("competitors", []) if isinstance(data, dict) else []
        for c in competitors:
            name = c.get("name", "")
            if team_name.lower() in name.lower() or name.lower() in team_name.lower():
                cid = c.get("id", "")
                with _id_cache_lock:
                    _team_id_cache[cache_key] = cid
                return cid
    else:
        data = _get(f"{base}/league/hierarchy.json")
        if not data:
            return None
        # Walk conferences → divisions → teams
        for conf in (data or {}).get("conferences", []):
            for div in conf.get("divisions", []):
                for team in div.get("teams", []):
                    name = team.get("name", "") or team.get("full_name", "")
                    if team_name.lower() in name.lower() or name.lower() in team_name.lower():
                        tid = team.get("id", "")
                        with _id_cache_lock:
                            _team_id_cache[cache_key] = tid
                        return tid
    return None


# ── Public API surface ─────────────────────────────────────────────────────────

def get_team_injuries(sport_key: str) -> list[dict]:
    """
    Fetch current injuries for a sport.
    Returns normalized list of {player, team, status, description}.
    """
    base = _sport_base(sport_key)
    if not base or sport_key in _SOCCER_SPORTS or sport_key == "mma_mixed_martial_arts":
        return []

    if sport_key == "americanfootball_nfl":
        # NFL injuries require week — use latest season/week from current schedule
        data = _get(f"{base}/games/current_week/schedule.json")
        if not data:
            return []
        week = data.get("week", {}).get("id", 1)
        year = data.get("year", 2024)
        season_type = data.get("season_type", "REG")
        data = _get(f"{base}/seasons/{year}/{season_type}/weeks/{week}/injuries.json")
    else:
        data = _get(f"{base}/league/injuries.json")

    if not data:
        return []

    injuries = []
    teams = data.get("teams", []) if isinstance(data, dict) else []
    for team in teams:
        team_name = team.get("name", "")
        for player in team.get("players", []):
            injuries.append({
                "player":      player.get("full_name", player.get("name", "")),
                "team":        team_name,
                "status":      player.get("status", player.get("injury_status", "")),
                "description": player.get("desc", player.get("injury", "")),
                "source":      "sportradar",
            })
    return injuries


def get_recent_form(sport_key: str, team_name: str, n: int = 5) -> list[dict]:
    """
    Return last N game results for a team.
    Soccer uses the native competitor/schedules endpoint.
    Other sports build from the season schedule (more API calls).
    Returns list of {date, home, away, home_score, away_score, result} dicts.
    """
    base = _sport_base(sport_key)
    if not base:
        return []

    if sport_key in _SOCCER_SPORTS:
        team_id = _find_team_id(sport_key, team_name)
        if not team_id:
            return []
        data = _get(f"{base}/competitors/{team_id}/schedules.json")
        if not data:
            return []
        results = []
        for item in (data.get("schedules") or []):
            sport_event = item.get("sport_event", {})
            result      = item.get("sport_event_status", {})
            if result.get("status") != "closed":
                continue
            competitors = sport_event.get("competitors", [])
            home = next((c["name"] for c in competitors if c.get("qualifier") == "home"), "")
            away = next((c["name"] for c in competitors if c.get("qualifier") == "away"), "")
            hs = result.get("home_score")
            as_ = result.get("away_score")
            won = (home.lower() in team_name.lower() or team_name.lower() in home.lower()) and hs is not None and as_ is not None and hs > as_
            lost = (away.lower() in team_name.lower() or team_name.lower() in away.lower()) and hs is not None and as_ is not None and as_ > hs
            results.append({
                "date":       sport_event.get("start_time", "")[:10],
                "home":       home,
                "away":       away,
                "home_score": hs,
                "away_score": as_,
                "result":     "W" if won else ("L" if lost else "D"),
                "source":     "sportradar",
            })
        return results[-n:]  # most recent N

    # For non-soccer: use team seasonal stats which includes win/loss record
    # (full game-by-game recent form requires many extra calls — too expensive on trial)
    team_id = _find_team_id(sport_key, team_name)
    if not team_id:
        return []
    from datetime import datetime
    year = datetime.utcnow().year
    data = _get(f"{base}/seasons/{year}/REG/teams/{team_id}/statistics.json")
    if not data:
        return []
    record = (data.get("own_record") or data.get("statistics") or {})
    wins   = record.get("wins", record.get("win", 0))
    losses = record.get("losses", record.get("loss", 0))
    avg    = record.get("average", {})
    return [{
        "source":       "sportradar",
        "type":         "season_stats",
        "wins":         wins,
        "losses":       losses,
        "win_pct":      round(wins / (wins + losses), 3) if (wins + losses) > 0 else None,
        "avg_points":   avg.get("points"),
        "avg_rebounds": avg.get("rebounds"),
        "avg_assists":  avg.get("assists"),
        "avg_goals":    avg.get("goals"),
    }]


def get_h2h(sport_key: str, team_a: str, team_b: str) -> list[dict]:
    """
    Return head-to-head results between two teams.
    Soccer and MMA have native endpoints.
    Other sports: return empty list (too expensive to construct without bulk data).
    """
    base = _sport_base(sport_key)
    if not base:
        return []

    if sport_key in _SOCCER_SPORTS or sport_key == "mma_mixed_martial_arts":
        id_a = _find_team_id(sport_key, team_a)
        id_b = _find_team_id(sport_key, team_b)
        if not id_a or not id_b:
            return []
        data = _get(f"{base}/competitors/{id_a}/versus/{id_b}/summaries.json")
        if not data:
            return []
        results = []
        for item in (data.get("summaries") or [])[:10]:
            se = item.get("sport_event", {})
            ss = item.get("sport_event_status", {})
            competitors = se.get("competitors", [])
            home = next((c["name"] for c in competitors if c.get("qualifier") == "home"), "")
            away = next((c["name"] for c in competitors if c.get("qualifier") == "away"), "")
            results.append({
                "date":       se.get("start_time", "")[:10],
                "home":       home,
                "away":       away,
                "home_score": ss.get("home_score"),
                "away_score": ss.get("away_score"),
                "winner":     ss.get("winner_id", ""),
                "source":     "sportradar",
            })
        return results

    return []


def get_player_stats(sport_key: str, player_name: str) -> dict:
    """
    Return player profile + season stats from Sportradar.
    Used to enrich player prop context.
    """
    base = _sport_base(sport_key)
    if not base or sport_key in _SOCCER_SPORTS:
        return {}

    # Player search via team hierarchy — expensive on trial (1 QPS)
    # Only attempt if player name looks specific (first + last name)
    parts = player_name.strip().split()
    if len(parts) < 2:
        return {}

    # Use league hierarchy to find player IDs within team rosters
    # Skip on MMA (fighter IDs found differently)
    if sport_key == "mma_mixed_martial_arts":
        return {}

    # This is a best-effort lookup — we just pull team hierarchy and scan rosters
    data = _get(f"{base}/league/hierarchy.json")
    if not data:
        return {}

    target = player_name.lower()
    for conf in (data.get("conferences") or []):
        for div in (conf.get("divisions") or []):
            for team in (div.get("teams") or []):
                for player in (team.get("players") or []):
                    pname = (player.get("full_name") or player.get("name") or "").lower()
                    if target in pname or pname in target:
                        pid = player.get("id", "")
                        if pid:
                            profile = _get(f"{base}/players/{pid}/profile.json")
                            return profile or {}
    return {}


def enrich_game_context(
    sport_key: str,
    home_team:  str,
    away_team:  str,
) -> dict:
    """
    Fetch all available Sportradar context for a game:
    - Injuries for this sport
    - Recent form for home + away team
    - H2H for soccer/MMA

    Returns a flat dict ready to merge into the data_hub context.
    Degrades gracefully — any fetch that fails returns empty data.
    """
    from src.core.config import SPORTRADAR_API_KEY
    if not SPORTRADAR_API_KEY:
        return {"available": False, "reason": "no_api_key"}

    result: dict = {"available": True, "source": "sportradar"}

    try:
        injuries = get_team_injuries(sport_key)
        if injuries:
            result["injuries"] = injuries

        home_form = get_recent_form(sport_key, home_team, n=5)
        away_form = get_recent_form(sport_key, away_team, n=5)
        if home_form:
            result["home_form"] = home_form
        if away_form:
            result["away_form"] = away_form

        h2h = get_h2h(sport_key, home_team, away_team)
        if h2h:
            result["h2h"] = h2h

    except Exception as e:
        logger.warning("Sportradar enrich_game_context failed [%s vs %s]: %s",
                       home_team, away_team, e)

    return result
