"""
ESPN data adapter.

Uses ESPN's semi-public APIs:
  - site.api.espn.com  — team/player stats, injuries, schedules, scores
  - sports.core.api.espn.com — deeper stats, historical data
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_SITE   = "https://site.api.espn.com/apis/site/v2/sports"
_CORE   = "https://sports.core.api.espn.com/v2/sports"
_CDN    = "https://cdn.espn.com/core"

# Maps our sport keys → ESPN sport/league path segments
SPORT_MAP = {
    # ── US Sports ────────────────────────────────────────────────────────────
    "basketball_nba":                       ("basketball", "nba"),
    "basketball_wnba":                      ("basketball", "wnba"),
    "americanfootball_nfl":                 ("football",   "nfl"),
    "baseball_mlb":                         ("baseball",   "mlb"),
    "icehockey_nhl":                        ("hockey",     "nhl"),
    "basketball_ncaab":                     ("basketball", "mens-college-basketball"),
    "americanfootball_ncaaf":               ("football",   "college-football"),
    # ── Soccer ───────────────────────────────────────────────────────────────
    "soccer_epl":                           ("soccer",     "eng.1"),
    "soccer_spain_la_liga":                 ("soccer",     "esp.1"),
    "soccer_germany_bundesliga":            ("soccer",     "ger.1"),
    "soccer_italy_serie_a":                 ("soccer",     "ita.1"),
    "soccer_france_ligue_one":              ("soccer",     "fra.1"),
    "soccer_usa_mls":                       ("soccer",     "usa.1"),
    "soccer_uefa_champs_league":            ("soccer",     "uefa.champions"),
    "soccer_uefa_europa_league":            ("soccer",     "uefa.europa"),
    "soccer_fifa_world_cup":                ("soccer",     "fifa.world"),
    "soccer_conmebol_copa_america":         ("soccer",     "conmebol.america"),
    "soccer_conmebol_copa_libertadores":    ("soccer",     "conmebol.libertadores"),
    # ── Combat Sports ────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":               ("mma",        "ufc"),
    "boxing_boxing":                        ("boxing",     "boxing"),
    # ── Golf — all majors + PGA/LPGA ─────────────────────────────────────────
    "golf_pga_tour":                        ("golf",       "pga"),
    "golf_masters_tournament":              ("golf",       "pga"),
    "golf_pga_championship":                ("golf",       "pga"),
    "golf_us_open":                         ("golf",       "pga"),
    "golf_the_open_championship":           ("golf",       "pga"),
    "golf_lpga":                            ("golf",       "lpga"),
    # ── Tennis — all Grand Slams + ATP/WTA ───────────────────────────────────
    "tennis_atp_french_open":               ("tennis",     "atp"),
    "tennis_wta_french_open":               ("tennis",     "wta"),
    "tennis_atp_wimbledon":                 ("tennis",     "atp"),
    "tennis_wta_wimbledon":                 ("tennis",     "wta"),
    "tennis_atp_us_open":                   ("tennis",     "atp"),
    "tennis_wta_us_open":                   ("tennis",     "wta"),
    "tennis_atp_australian_open":           ("tennis",     "atp"),
    "tennis_wta_australian_open":           ("tennis",     "wta"),
}


def _path(sport_key: str) -> tuple[str, str] | None:
    return SPORT_MAP.get(sport_key)


# ── Injuries ───────────────────────────────────────────────────────────────────

def fetch_injuries(sport_key: str) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/injuries")
    if not data:
        return []

    out = []
    for team_entry in data.get("injuries", []):
        team_name = team_entry.get("team", {}).get("displayName", "Unknown")
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete", {})
            out.append({
                "source":   "espn",
                "player":   athlete.get("displayName", "Unknown"),
                "team":     team_name,
                "position": athlete.get("position", {}).get("abbreviation", ""),
                "status":   inj.get("status", "unknown").lower(),
                "details":  inj.get("shortComment", ""),
                "sport":    sport_key,
            })
    return out


# ── Team stats ─────────────────────────────────────────────────────────────────

def fetch_team_stats(sport_key: str, team_id: str) -> dict:
    seg = _path(sport_key)
    if not seg:
        return {}
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/teams/{team_id}/statistics")
    if not data:
        return {}

    stats = {}
    for cat in data.get("results", {}).get("stats", {}).get("categories", []):
        for stat in cat.get("stats", []):
            stats[stat.get("name", "")] = stat.get("displayValue", "")
    return {"team_id": team_id, "sport": sport_key, "stats": stats, "source": "espn"}


def fetch_team_roster(sport_key: str, team_id: str) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/teams/{team_id}/roster")
    if not data:
        return []

    players = []
    for group in data.get("athletes", []):
        for athlete in group.get("items", []):
            players.append({
                "id":       athlete.get("id"),
                "name":     athlete.get("displayName"),
                "position": athlete.get("position", {}).get("abbreviation", ""),
                "jersey":   athlete.get("jersey", ""),
                "status":   athlete.get("status", {}).get("name", "Active"),
            })
    return players


def fetch_teams(sport_key: str) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/teams", params={"limit": 100})
    if not data:
        return []

    return [
        {
            "id":           t.get("id"),
            "name":         t.get("displayName"),
            "abbreviation": t.get("abbreviation"),
            "location":     t.get("location"),
            "sport":        sport_key,
            "source":       "espn",
        }
        for t in (
            entry.get("team", entry)
            for entry in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
            if entry.get("team")
        )
    ]


# ── Player stats ───────────────────────────────────────────────────────────────

def fetch_player_stats(sport_key: str, player_id: str) -> dict:
    seg = _path(sport_key)
    if not seg:
        return {}
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/athletes/{player_id}/statistics")
    if not data:
        return {}

    stats = {}
    for split in data.get("splits", {}).get("categories", []):
        for stat in split.get("stats", []):
            stats[stat.get("name", "")] = stat.get("displayValue", "")
    return {"player_id": player_id, "sport": sport_key, "stats": stats, "source": "espn"}


def search_player(name: str, sport_key: str) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/athletes", params={"limit": 10, "search": name})
    if not data:
        return []
    return [
        {"id": a.get("id"), "name": a.get("displayName"), "team": a.get("team", {}).get("displayName", ""), "source": "espn"}
        for a in data.get("athletes", [])
    ]


# ── Schedule & scores ──────────────────────────────────────────────────────────

def fetch_scoreboard(sport_key: str) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/scoreboard")
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        status = event.get("status", {}).get("type", {})
        games.append({
            "id":             event.get("id"),
            "name":           event.get("name"),
            "commence_time":  event.get("date"),
            "home_team":      home.get("team", {}).get("displayName", ""),
            "away_team":      away.get("team", {}).get("displayName", ""),
            "home_score":     home.get("score"),
            "away_score":     away.get("score"),
            "status":         status.get("description", ""),
            "completed":      status.get("completed", False),
            "sport":          sport_key,
            "source":         "espn",
        })
    return games


# ── News ───────────────────────────────────────────────────────────────────────

def fetch_news(sport_key: str, limit: int = 10) -> list[dict]:
    seg = _path(sport_key)
    if not seg:
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/news", params={"limit": limit})
    if not data:
        return []

    return [
        {
            "headline":    a.get("headline", ""),
            "description": a.get("description", ""),
            "published":   a.get("published", ""),
            "sport":       sport_key,
            "source":      "espn",
        }
        for a in data.get("articles", [])
    ]


# ── Team recent form (last N games) ───────────────────────────────────────────

def fetch_team_record(sport_key: str, team_id: str) -> dict:
    seg = _path(sport_key)
    if not seg:
        return {}
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/teams/{team_id}")
    if not data:
        return {}

    team = data.get("team", {})
    record = team.get("record", {}).get("items", [{}])[0]
    stats  = {s.get("name"): s.get("value") for s in record.get("stats", [])}
    return {
        "team_id":  team_id,
        "name":     team.get("displayName", ""),
        "wins":     stats.get("wins", 0),
        "losses":   stats.get("losses", 0),
        "win_pct":  stats.get("winPercent", 0.0),
        "streak":   stats.get("streak", ""),
        "source":   "espn",
    }
