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
    # ── US Men's Sports ───────────────────────────────────────────────────────
    "basketball_nba":                           ("basketball", "nba"),
    "basketball_nba_summer_league":             ("basketball", "nba"),
    "basketball_ncaab":                         ("basketball", "mens-college-basketball"),
    "americanfootball_nfl":                     ("football",   "nfl"),
    "americanfootball_ncaaf":                   ("football",   "college-football"),
    "baseball_mlb":                             ("baseball",   "mlb"),
    "icehockey_nhl":                            ("hockey",     "nhl"),
    # ── US Women's Sports ─────────────────────────────────────────────────────
    "basketball_wnba":                          ("basketball", "wnba"),
    "basketball_wncaab":                        ("basketball", "womens-college-basketball"),
    "icehockey_pwhl":                           ("hockey",     "pwhl"),
    "soccer_usa_nwsl":                          ("soccer",     "usa.nwsl"),
    # ── Soccer — Men's Top Leagues ────────────────────────────────────────────
    "soccer_epl":                               ("soccer",     "eng.1"),
    "soccer_england_league1":                   ("soccer",     "eng.3"),
    "soccer_england_league2":                   ("soccer",     "eng.4"),
    "soccer_england_efl_cup":                   ("soccer",     "eng.league_cup"),
    "soccer_spain_la_liga":                     ("soccer",     "esp.1"),
    "soccer_spain_segunda_division":            ("soccer",     "esp.2"),
    "soccer_germany_bundesliga":                ("soccer",     "ger.1"),
    "soccer_germany_bundesliga2":               ("soccer",     "ger.2"),
    "soccer_italy_serie_a":                     ("soccer",     "ita.1"),
    "soccer_italy_serie_b":                     ("soccer",     "ita.2"),
    "soccer_france_ligue_one":                  ("soccer",     "fra.1"),
    "soccer_france_ligue_two":                  ("soccer",     "fra.2"),
    "soccer_netherlands_eredivisie":            ("soccer",     "ned.1"),
    "soccer_portugal_primeira_liga":            ("soccer",     "por.1"),
    "soccer_turkey_super_league":               ("soccer",     "tur.1"),
    "soccer_spl":                               ("soccer",     "sco.1"),
    "soccer_belgium_first_div":                 ("soccer",     "bel.1"),
    "soccer_usa_mls":                           ("soccer",     "usa.1"),
    "soccer_mexico_ligamx":                     ("soccer",     "mex.1"),
    "soccer_brazil_campeonato":                 ("soccer",     "bra.1"),
    "soccer_argentina_primera_division":        ("soccer",     "arg.1"),
    "soccer_chile_campeonato":                  ("soccer",     "chi.1"),
    "soccer_colombia_primera_a":                ("soccer",     "col.1"),
    "soccer_china_superleague":                 ("soccer",     "chn.1"),
    "soccer_japan_j_league":                    ("soccer",     "jpn.1"),
    "soccer_australia_aleague":                 ("soccer",     "aus.1"),
    "soccer_sweden_allsvenskan":                ("soccer",     "swe.1"),
    "soccer_norway_eliteserien":                ("soccer",     "nor.1"),
    "soccer_denmark_superliga":                 ("soccer",     "den.1"),
    "soccer_finland_veikkausliiga":             ("soccer",     "fin.1"),
    "soccer_austria_bundesliga":                ("soccer",     "aut.1"),
    "soccer_switzerland_superleague":           ("soccer",     "sui.1"),
    "soccer_greece_super_league":               ("soccer",     "gre.1"),
    "soccer_russia_premier_league":             ("soccer",     "rus.1"),
    "soccer_ukraine_premier_league":            ("soccer",     "ukr.1"),
    # ── Soccer — Cups & International ────────────────────────────────────────
    "soccer_uefa_champs_league":                ("soccer",     "uefa.champions"),
    "soccer_uefa_europa_league":                ("soccer",     "uefa.europa"),
    "soccer_uefa_europa_conference_league":     ("soccer",     "uefa.europa.conf"),
    "soccer_uefa_nations_league":               ("soccer",     "uefa.nations"),
    "soccer_fifa_world_cup":                    ("soccer",     "fifa.world"),
    "soccer_conmebol_copa_america":             ("soccer",     "conmebol.america"),
    "soccer_conmebol_copa_libertadores":        ("soccer",     "conmebol.libertadores"),
    "soccer_conmebol_copa_sudamericana":        ("soccer",     "conmebol.sudamericana"),
    "soccer_africa_cup_of_nations":             ("soccer",     "caf.nations"),
    "soccer_concacaf_champions_cup":            ("soccer",     "concacaf.champions"),
    "soccer_concacaf_gold_cup":                 ("soccer",     "concacaf.gold"),
    # ── Soccer — Women's ─────────────────────────────────────────────────────
    "soccer_fifa_womens_world_cup":             ("soccer",     "fifa.wwc"),
    "soccer_uefa_womens_euro":                  ("soccer",     "uefa.weuro"),
    "soccer_england_womens_super_league":       ("soccer",     "eng.w.1"),
    "soccer_germany_frauen_bundesliga":         ("soccer",     "ger.w.1"),
    "soccer_spain_primera_rfef_women":          ("soccer",     "esp.w.1"),
    "soccer_france_division_1_feminine":        ("soccer",     "fra.w.1"),
    # ── Combat Sports ────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":                   ("mma",        "ufc"),
    "boxing_boxing":                            ("boxing",     "boxing"),
    # ── Golf — Men's + Women's ────────────────────────────────────────────────
    "golf_pga_tour":                            ("golf",       "pga"),
    "golf_masters_tournament":                  ("golf",       "pga"),
    "golf_pga_championship":                    ("golf",       "pga"),
    "golf_us_open":                             ("golf",       "pga"),
    "golf_the_open_championship":               ("golf",       "pga"),
    "golf_lpga":                                ("golf",       "lpga"),
    "golf_dp_world_tour":                       ("golf",       "dpwt"),
    # ── Tennis — Men's ATP + Women's WTA ─────────────────────────────────────
    "tennis_atp_french_open":                   ("tennis",     "atp"),
    "tennis_wta_french_open":                   ("tennis",     "wta"),
    "tennis_atp_wimbledon":                     ("tennis",     "atp"),
    "tennis_wta_wimbledon":                     ("tennis",     "wta"),
    "tennis_atp_us_open":                       ("tennis",     "atp"),
    "tennis_wta_us_open":                       ("tennis",     "wta"),
    "tennis_atp_australian_open":               ("tennis",     "atp"),
    "tennis_wta_australian_open":               ("tennis",     "wta"),
    "tennis_atp":                               ("tennis",     "atp"),
    "tennis_wta":                               ("tennis",     "wta"),
    # ── Cricket ───────────────────────────────────────────────────────────────
    "cricket_test_match":                       ("cricket",    "icc"),
    "cricket_odi":                              ("cricket",    "icc"),
    "cricket_ipl":                              ("cricket",    "ipl"),
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "rugbyunion_six_nations":                   ("rugby",      "irb.6nations"),
    "rugbyunion_world_cup":                     ("rugby",      "irb.world"),
    "rugbyleague_nrl":                          ("rugby-league", "nrl"),
    # ── Australian Rules ──────────────────────────────────────────────────────
    "aussierules_afl":                          ("australian-football", "afl"),
}


def _path(sport_key: str) -> tuple[str, str] | None:
    return SPORT_MAP.get(sport_key)


# ── Injuries ───────────────────────────────────────────────────────────────────

_DESIGNATION_MAP = {
    "out":             "OUT",
    "doubtful":        "DOUBTFUL",
    "questionable":    "QUESTIONABLE",
    "probable":        "PROBABLE",
    "day-to-day":      "DAY-TO-DAY",
    "injured reserve": "IR",
    "ir":              "IR",
    "pup":             "PUP",
    "suspended":       "SUSPENDED",
    "active":          "ACTIVE",
}


def fetch_injuries(sport_key: str) -> list[dict]:
    import logging as _log
    _logger = _log.getLogger(__name__)
    seg = _path(sport_key)
    if not seg:
        _logger.debug("fetch_injuries: no ESPN segment for sport_key=%s", sport_key)
        return []
    sport, league = seg
    data = get_json(f"{_SITE}/{sport}/{league}/injuries")
    if not data:
        _logger.warning("fetch_injuries: empty response from ESPN for %s/%s", sport, league)
        return []

    out = []
    for team_entry in data.get("injuries", []):
        team_name = team_entry.get("team", {}).get("displayName", "Unknown")
        for inj in team_entry.get("injuries", []):
            athlete = inj.get("athlete", {})
            raw_status = inj.get("status", "unknown").lower().strip()
            designation = _DESIGNATION_MAP.get(raw_status, raw_status.upper())
            # Flag high-impact injuries so AI weighs them correctly
            is_out = designation in ("OUT", "IR", "PUP", "SUSPENDED")
            out.append({
                "source":      "espn",
                "player":      athlete.get("displayName", "Unknown"),
                "team":        team_name,
                "position":    athlete.get("position", {}).get("abbreviation", ""),
                "status":      raw_status,
                "designation": designation,          # normalized: OUT / DOUBTFUL / QUESTIONABLE / IR
                "is_out":      is_out,               # True = confirmed not playing
                "details":     inj.get("shortComment", ""),
                "sport":       sport_key,
            })

    if not out:
        _logger.info("fetch_injuries: no injuries found for %s/%s", sport, league)
    else:
        out_count = sum(1 for i in out if i["is_out"])
        _logger.info("fetch_injuries: %d injuries (%d OUT/IR) for %s", len(out), out_count, sport_key)
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
