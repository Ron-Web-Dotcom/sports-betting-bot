"""
SofaScore adapter.

SofaScore provides real-time scores, form tables, head-to-head records,
player statistics, standings, and match event timelines across virtually
every sport (soccer, basketball, NFL, NHL, MLB, tennis, MMA, esports, etc.).

Uses SofaScore's unofficial public API — no key required.
Endpoint base: https://api.sofascore.com/api/v1

Key endpoints used:
  GET /sport/{sport}/events/live                 live matches
  GET /sport/{sport}/scheduled-events/{date}     matches on a given date (YYYY-MM-DD)
  GET /event/{id}/statistics                     full match stats
  GET /event/{id}/h2h/events                     head-to-head history
  GET /event/{id}/form                           recent form for both teams
  GET /team/{id}/events/last/0                   last 5 team matches
  GET /team/{id}/standings/seasons               standings
  GET /player/{id}/statistics/season/{season_id} season stats
  GET /team/{id}/players                         squad list with positions
"""
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from src.apis.base import get_json
from src.core.timezone import et_naive, ET

logger = logging.getLogger(__name__)

_BASE = "https://api.sofascore.com/api/v1"

# Maps our internal sport_key → SofaScore sport slug
SPORT_MAP = {
    # ── US Sports ─────────────────────────────────────────────────────────────
    "americanfootball_nfl":                  "american-football",
    "americanfootball_ncaaf":                "american-football",
    "basketball_nba":                        "basketball",
    "basketball_wnba":                       "basketball",
    "basketball_ncaab":                      "basketball",
    "basketball_wncaab":                     "basketball",
    "baseball_mlb":                          "baseball",
    "icehockey_nhl":                         "ice-hockey",
    # ── Soccer — Top Leagues ──────────────────────────────────────────────────
    "soccer_epl":                            "football",
    "soccer_spain_la_liga":                  "football",
    "soccer_germany_bundesliga":             "football",
    "soccer_italy_serie_a":                  "football",
    "soccer_france_ligue_one":               "football",
    "soccer_usa_mls":                        "football",
    "soccer_netherlands_eredivisie":         "football",
    "soccer_portugal_primeira_liga":         "football",
    "soccer_mexico_ligamx":                  "football",
    "soccer_argentina_primera_division":     "football",
    "soccer_brazil_campeonato":              "football",
    "soccer_turkey_super_league":            "football",
    "soccer_spl":                            "football",   # Scottish Premiership
    # ── Soccer — Cups & International ────────────────────────────────────────
    "soccer_uefa_champs_league":             "football",
    "soccer_uefa_europa_league":             "football",
    "soccer_uefa_europa_conference_league":  "football",
    "soccer_fifa_world_cup":                 "football",
    "soccer_fifa_club_world_cup":            "football",   # Club World Cup 2026
    "soccer_conmebol_copa_america":          "football",
    "soccer_conmebol_copa_libertadores":     "football",
    "soccer_conmebol_copa_sudamericana":     "football",   # Copa Sudamericana
    "soccer_africa_cup_of_nations":          "football",
    # ── Women's Soccer ────────────────────────────────────────────────────────
    "soccer_usa_nwsl":                       "football",
    "soccer_fifa_womens_world_cup":          "football",
    "soccer_uefa_womens_champs_league":      "football",
    "soccer_england_wsl":                    "football",
    "soccer_germany_frauen_bundesliga":      "football",
    "soccer_spain_liga_f":                   "football",
    "soccer_france_d1_feminine":             "football",
    "soccer_italy_serie_a_feminine":         "football",
    # ── Golf ─────────────────────────────────────────────────────────────────
    "golf_pga_tour":                         "golf",
    "golf_masters_tournament_winner":        "golf",
    "golf_pga_championship_winner":          "golf",
    "golf_us_open_winner":                   "golf",
    "golf_the_open_championship_winner":     "golf",
    "golf_lpga":                             "golf",
    # ── Combat Sports ────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":                "mma",
    "boxing_boxing":                         "boxing",
    # ── Tennis — Grand Slams + Active Tour Events ─────────────────────────────
    "tennis_atp_french_open":                "tennis",
    "tennis_wta_french_open":                "tennis",
    "tennis_atp_wimbledon":                  "tennis",
    "tennis_wta_wimbledon":                  "tennis",
    "tennis_atp_us_open":                    "tennis",
    "tennis_wta_us_open":                    "tennis",
    "tennis_atp_australian_open":            "tennis",
    "tennis_wta_aus_open_singles":           "tennis",
    "tennis_atp_queens_club_champ":          "tennis",
    "tennis_atp_halle_open":                 "tennis",
    "tennis_wta_german_open":                "tennis",
    # ── Women's Hockey ────────────────────────────────────────────────────────
    "icehockey_pwhl":                        "ice-hockey",
    # ── Aussie Rules (Men's + Women's) ───────────────────────────────────────
    "aussierules_afl":                       "australian-football",
    "aussierules_aflw":                      "australian-football",
    # ── Rugby (Men's + Women's) ───────────────────────────────────────────────
    "rugbyunion_world_cup":                  "rugby",
    "rugbyunion_women_world_cup":            "rugby",
    "rugbyleague_nrl":                       "rugby-league",
    "rugbyleague_nrl_state_of_origin":       "rugby-league",
    # ── Cricket (Men's + Women's) ─────────────────────────────────────────────
    "cricket_icc_world_cup":                 "cricket",
    "cricket_ipl":                           "cricket",
    "cricket_icc_womens_t20_wc":             "cricket",
    "cricket_test_match":                    "cricket",
    "cricket_odi":                           "cricket",
    "cricket_international_t20":             "cricket",
    "cricket_t20_world_cup_womens":          "cricket",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Referer":          "https://www.sofascore.com/",
    "Origin":           "https://www.sofascore.com",
    "Cache-Control":    "no-cache",
    "Pragma":           "no-cache",
    "sec-ch-ua":        '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}


def _slug(sport_key: str) -> str | None:
    return SPORT_MAP.get(sport_key)


def _get(path: str) -> dict | list | None:
    return get_json(f"{_BASE}{path}", headers=_HEADERS)


# ── Scheduled events ──────────────────────────────────────────────────────────

def get_scheduled_events(sport_key: str, date: str | None = None) -> list[dict]:
    """
    Return matches scheduled for a given date (default: today).
    date: "YYYY-MM-DD" string
    """
    slug = _slug(sport_key)
    if not slug:
        return []
    date = date or et_naive().strftime("%Y-%m-%d")
    data = _get(f"/sport/{slug}/scheduled-events/{date}")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    return [_normalise_event(e, sport_key) for e in events]


def get_live_events(sport_key: str) -> list[dict]:
    """Return currently live matches."""
    slug = _slug(sport_key)
    if not slug:
        return []
    data = _get(f"/sport/{slug}/events/live")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    return [_normalise_event(e, sport_key) for e in events]


def _normalise_event(e: dict, sport_key: str) -> dict:
    home = e.get("homeTeam", {})
    away = e.get("awayTeam", {})
    score = e.get("homeScore", {}), e.get("awayScore", {})
    status = e.get("status", {})
    return {
        "id":             str(e.get("id", "")),
        "sport":          sport_key,
        "home_team":      home.get("name", ""),
        "home_team_id":   str(home.get("id", "")),
        "away_team":      away.get("name", ""),
        "away_team_id":   str(away.get("id", "")),
        "home_score":     score[0].get("current"),
        "away_score":     score[1].get("current"),
        "status":         status.get("description", ""),
        "status_type":    status.get("type", ""),
        "commence_time":  _epoch_to_iso(e.get("startTimestamp")),
        "tournament":     e.get("tournament", {}).get("name", ""),
        "season":         e.get("season", {}).get("name", ""),
        "source":         "sofascore",
    }


def get_active_sports_today() -> set[str]:
    """
    Check Sofascore for ALL sports that have games scheduled or live today.
    Runs in parallel across all sport_keys in SPORT_MAP.
    Returns a set of Odds API sport_keys that are active.
    Used to gate Odds API calls — only fetch for confirmed active sports.
    """
    from concurrent.futures import ThreadPoolExecutor
    today = et_naive().strftime("%Y-%m-%d")
    active: set[str] = set()

    def _check(sport_key: str) -> str | None:
        slug = SPORT_MAP.get(sport_key)
        if not slug:
            return None
        # Check scheduled events first (cheaper), then live
        data = _get(f"/sport/{slug}/scheduled-events/{today}")
        events = (data or {}).get("events", []) if isinstance(data, dict) else (data or [])
        if events:
            return sport_key
        data = _get(f"/sport/{slug}/events/live")
        events = (data or {}).get("events", []) if isinstance(data, dict) else (data or [])
        return sport_key if events else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_check, sk): sk for sk in SPORT_MAP}
        for fut in futures:
            result = fut.result()
            if result:
                active.add(result)

    logger.info("Sofascore active sports today: %s", sorted(active))
    return active


def _epoch_to_iso(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=ET).replace(tzinfo=None).isoformat()
    except (OSError, ValueError):
        return ""


# ── Head-to-head ──────────────────────────────────────────────────────────────

def get_h2h(event_id: str) -> list[dict]:
    """
    Return last N head-to-head results between the two teams in a given event.
    event_id: SofaScore event (match) ID.
    """
    data = _get(f"/event/{event_id}/h2h/events")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    results = []
    for e in events:
        home = e.get("homeTeam", {})
        away = e.get("awayTeam", {})
        hs = e.get("homeScore", {}).get("current")
        as_ = e.get("awayScore", {}).get("current")
        results.append({
            "home_team":  home.get("name", ""),
            "away_team":  away.get("name", ""),
            "home_score": hs,
            "away_score": as_,
            "date":       _epoch_to_iso(e.get("startTimestamp")),
            "winner":     (
                "home" if (hs is not None and as_ is not None and hs > as_) else
                "away" if (hs is not None and as_ is not None and as_ > hs) else
                "draw"
            ),
            "source":     "sofascore",
        })
    return results


# ── Team recent form ──────────────────────────────────────────────────────────

def get_team_form(event_id: str) -> dict:
    """
    Return the recent form string for both teams in a given event.
    e.g. {"home": "WWLDW", "away": "DLWWW"}
    """
    data = _get(f"/event/{event_id}/form")
    if not data:
        return {}
    home_form = "".join(f.get("value", "?") for f in data.get("homeTeam", []))
    away_form = "".join(f.get("value", "?") for f in data.get("awayTeam", []))
    return {"home": home_form, "away": away_form, "source": "sofascore"}


def get_team_last_events(team_id: str, page: int = 0) -> list[dict]:
    """
    Return a team's most recent N completed events.
    page=0 → most recent page (≈5 matches).
    """
    data = _get(f"/team/{team_id}/events/last/{page}")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    results = []
    for e in events:
        home = e.get("homeTeam", {})
        away = e.get("awayTeam", {})
        hs = e.get("homeScore", {}).get("current")
        as_ = e.get("awayScore", {}).get("current")
        results.append({
            "home_team":  home.get("name", ""),
            "away_team":  away.get("name", ""),
            "home_score": hs,
            "away_score": as_,
            "date":       _epoch_to_iso(e.get("startTimestamp")),
            "source":     "sofascore",
        })
    return results


# ── Match statistics ──────────────────────────────────────────────────────────

def get_event_statistics(event_id: str) -> dict:
    """
    Return detailed in-match statistics: possession, shots, passes, etc.
    Useful for post-game CLV analysis and model calibration.
    """
    data = _get(f"/event/{event_id}/statistics")
    if not data:
        return {}

    stats_groups = data if isinstance(data, list) else data.get("statistics", [])
    result: dict = {"source": "sofascore"}
    for group in stats_groups:
        period = group.get("period", "ALL")
        for item in group.get("groups", []):
            for stat in item.get("statisticsItems", []):
                name = stat.get("name", "").lower().replace(" ", "_")
                key = f"{period}_{name}" if period != "ALL" else name
                result[key] = {
                    "home": stat.get("home"),
                    "away": stat.get("away"),
                }
    return result


# ── Player statistics ─────────────────────────────────────────────────────────

def get_player_stats(player_id: str, season_id: str) -> dict:
    """Return season statistics for a specific player."""
    data = _get(f"/player/{player_id}/statistics/season/{season_id}")
    if not data:
        return {}
    stats = data if isinstance(data, dict) else data.get("statistics", {})
    return {"player_id": player_id, "season_id": season_id,
            "stats": stats, "source": "sofascore"}


def search_player(name: str, sport_key: str) -> list[dict]:
    """Search for a player by name across all sports."""
    slug = _slug(sport_key)
    data = _get(f"/search/all/?q={quote(name)}")
    if not data:
        return []
    players = []
    for item in data.get("players", {}).get("hits", []):
        e = item.get("entity", {})
        team = e.get("team", {})
        players.append({
            "id":       str(e.get("id", "")),
            "name":     e.get("name", ""),
            "team":     team.get("name", ""),
            "position": e.get("position", ""),
            "source":   "sofascore",
        })
    return players


def get_team_squad(team_id: str) -> list[dict]:
    """Return the full squad (players) for a team."""
    data = _get(f"/team/{team_id}/players")
    if not data:
        return []
    players = []
    for entry in data.get("players", []):
        p = entry.get("player", {})
        players.append({
            "id":       str(p.get("id", "")),
            "name":     p.get("name", ""),
            "position": p.get("position", ""),
            "jersey":   p.get("jerseyNumber", ""),
            "country":  p.get("country", {}).get("name", ""),
            "source":   "sofascore",
        })
    return players


# ── Standings ─────────────────────────────────────────────────────────────────

def get_standings(tournament_id: str, season_id: str) -> list[dict]:
    """Return league/tournament standings table."""
    data = _get(f"/tournament/{tournament_id}/season/{season_id}/standings/total")
    if not data:
        return []
    rows = data if isinstance(data, list) else (data.get("standings") or [{}])[0].get("rows", [])
    result = []
    for row in rows:
        team = row.get("team", {})
        result.append({
            "position":     row.get("position"),
            "team":         team.get("name", ""),
            "team_id":      str(team.get("id", "")),
            "played":       row.get("matches"),
            "wins":         row.get("wins"),
            "draws":        row.get("draws"),
            "losses":       row.get("losses"),
            "goals_for":    row.get("scoresFor"),
            "goals_against": row.get("scoresAgainst"),
            "points":       row.get("points"),
            "source":       "sofascore",
        })
    return result


# ── Convenience: enrich a game context dict ───────────────────────────────────

def enrich_game_context(
    sport_key: str,
    home_team: str,
    away_team: str,
    game_time: str,
) -> dict:
    """
    Find the SofaScore event matching this game and return combined context:
    h2h history, form, and match statistics when available.

    Matches by team name substring — best-effort.
    """
    date = game_time[:10] if game_time else et_naive().strftime("%Y-%m-%d")
    events = get_scheduled_events(sport_key, date)

    matched = None
    for ev in events:
        if (home_team.lower() in ev["home_team"].lower() or
                ev["home_team"].lower() in home_team.lower() or
                away_team.lower() in ev["away_team"].lower() or
                ev["away_team"].lower() in away_team.lower()):
            matched = ev
            break

    if not matched:
        return {"available": False, "sport": sport_key, "source": "sofascore"}

    eid = matched["id"]
    context = {
        "available":    True,
        "event":        matched,
        "h2h":          get_h2h(eid),
        "form":         get_team_form(eid),
        "source":       "sofascore",
    }
    return context
