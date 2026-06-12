"""
TheSportsDB universal adapter.

Free tier (key=3) — no registration required.
Covers: NBA, WNBA, NFL, MLB, NHL, MLS, Premier League, La Liga,
        Bundesliga, Serie A, Ligue 1, Champions League, FIFA World Cup,
        Copa Libertadores, NWSL, Women's World Cup, and 800+ more leagues.

Provides: team recent form, W/L record, streak, last N results.
Rate limit: ~30 req/min on free tier — acceptable for our use.
"""
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# Internal sport_key → TheSportsDB league search name (for search_all_teams)
# Used as a hint — direct team name search works without this
_LEAGUE_HINT: dict[str, str] = {
    # ── US Sports ─────────────────────────────────────────────────────────────
    "basketball_nba":                       "NBA",
    "basketball_wnba":                      "WNBA",
    "americanfootball_nfl":                 "NFL",
    "baseball_mlb":                         "MLB",
    "icehockey_nhl":                        "NHL",
    "basketball_ncaab":                     "NCAA Basketball",
    "americanfootball_ncaaf":               "NCAA Football",
    # ── Soccer ────────────────────────────────────────────────────────────────
    "soccer_epl":                           "English Premier League",
    "soccer_spain_la_liga":                 "Spanish La Liga",
    "soccer_germany_bundesliga":            "German Bundesliga",
    "soccer_italy_serie_a":                 "Italian Serie A",
    "soccer_france_ligue_one":              "French Ligue 1",
    "soccer_usa_mls":                       "Major League Soccer",
    "soccer_netherlands_eredivisie":        "Dutch Eredivisie",
    "soccer_portugal_primeira_liga":        "Portuguese Primeira Liga",
    "soccer_uefa_champs_league":            "UEFA Champions League",
    "soccer_uefa_europa_league":            "UEFA Europa League",
    "soccer_fifa_world_cup":               "FIFA World Cup",
    "soccer_conmebol_copa_libertadores":    "Copa Libertadores",
    "soccer_conmebol_copa_america":         "Copa America",
    "soccer_usa_nwsl":                      "NWSL",
    "soccer_fifa_womens_world_cup":         "FIFA Women's World Cup",
    "soccer_england_wsl":                   "Women's Super League",
    "soccer_mexico_ligamx":                 "Liga MX",
    "soccer_turkey_super_league":           "Turkish Super League",
    # ── Other ─────────────────────────────────────────────────────────────────
    "icehockey_pwhl":                       "PWHL",
    "rugbyleague_nrl":                      "NRL",
    "aussierules_afl":                      "AFL",
    "aussierules_aflw":                     "AFLW",
    "mma_mixed_martial_arts":               "UFC",
}

# In-memory cache: "sport_key:team_name" → team_id
_team_id_cache: dict[str, str] = {}


def _get(path: str) -> dict | None:
    from src.apis.base import get_json
    return get_json(f"{_BASE}{path}")


def find_team_id(team_name: str, sport_key: str = "") -> str | None:
    """
    Find TheSportsDB team ID for a team name.
    Uses direct name search — works for all sports without knowing league ID.
    """
    cache_key = f"{sport_key}:{team_name.lower()}"
    if cache_key in _team_id_cache:
        return _team_id_cache[cache_key]

    data = _get(f"/searchteams.php?t={quote(team_name)}")
    teams = (data or {}).get("teams") or []
    if not teams:
        return None

    name_l = team_name.lower()
    league_hint = _LEAGUE_HINT.get(sport_key, "").lower()

    # Prefer exact sport/league match
    for t in teams:
        tname  = (t.get("strTeam") or "").lower()
        league = (t.get("strLeague") or "").lower()
        if (name_l in tname or tname in name_l):
            if not league_hint or league_hint in league or league in league_hint:
                tid = str(t.get("idTeam", ""))
                if tid:
                    _team_id_cache[cache_key] = tid
                    return tid

    # Fallback: first name match regardless of league
    for t in teams:
        tname = (t.get("strTeam") or "").lower()
        if name_l in tname or tname in name_l:
            tid = str(t.get("idTeam", ""))
            if tid:
                _team_id_cache[cache_key] = tid
                return tid

    return None


def get_team_form(team_name: str, sport_key: str = "", n: int = 10) -> dict:
    """
    Pull recent form for any team from TheSportsDB.
    Returns: wins, losses, form string (e.g. WWLWW), streak (e.g. W2), source
    """
    team_id = find_team_id(team_name, sport_key)
    if not team_id:
        logger.debug("TheSportsDB: team '%s' not found [%s]", team_name, sport_key)
        return {}

    data = _get(f"/eventslast.php?id={team_id}")
    events = (data or {}).get("results") or []
    if not events:
        return {}

    results = []
    for ev in events[:n]:
        hs  = ev.get("intHomeScore")
        as_ = ev.get("intAwayScore")
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (ValueError, TypeError):
            continue
        home_team = (ev.get("strHomeTeam") or "").lower()
        name_l    = team_name.lower()
        is_home   = name_l in home_team or any(w in home_team for w in name_l.split() if len(w) > 3)
        won = (hs > as_) if is_home else (as_ > hs)
        draw = hs == as_
        results.append("D" if draw else ("W" if won else "L"))

    if not results:
        return {}

    form_str = "".join(results)
    wins   = form_str.count("W")
    losses = form_str.count("L")
    draws  = form_str.count("D")

    streak_char = results[0]
    streak_cnt  = 0
    for r in results:
        if r == streak_char:
            streak_cnt += 1
        else:
            break

    win_pct = round(wins / (wins + losses + draws), 3) if (wins + losses + draws) > 0 else 0.0

    return {
        "team":           team_name,
        "wins":           wins,
        "losses":         losses,
        "draws":          draws,
        "form":           form_str,
        "streak":         f"{streak_char}{streak_cnt}",
        "win_pct":        win_pct,
        "games_played":   len(results),
        "source":         "thesportsdb",
    }


def enrich_game_context(sport_key: str, home_team: str, away_team: str) -> dict:
    """
    Pull recent form for both teams in a matchup.
    Returns dict ready for injection into data_hub context.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home": (get_team_form, (home_team, sport_key, 10)),
        "away": (get_team_form, (away_team, sport_key, 10)),
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

    home_form = results.get("home", {})
    away_form = results.get("away", {})

    if not home_form and not away_form:
        return {}

    return {
        "available":       True,
        "home_form":       home_form,
        "away_form":       away_form,
        "source":          "thesportsdb",
    }
