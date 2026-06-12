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
    # ── Soccer — Women's Leagues ──────────────────────────────────────────────
    "soccer_germany_frauen_bundesliga":     "Frauen-Bundesliga",
    "soccer_spain_liga_f":                  "Liga F",
    "soccer_france_d1_feminine":            "D1 Féminine",
    "soccer_italy_serie_a_feminine":        "Serie A Femminile",
    "soccer_uefa_womens_champs_league":     "UEFA Women's Champions League",
    # ── Soccer — More Leagues ─────────────────────────────────────────────────
    "soccer_argentina_primera_division":    "Argentine Primera División",
    "soccer_brazil_campeonato":             "Brazilian Série A",
    "soccer_spl":                           "Scottish Premiership",
    "soccer_africa_cup_of_nations":         "African Cup of Nations",
    "soccer_conmebol_copa_libertadores":    "Copa Libertadores",
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "rugbyleague_nrl":                      "NRL",
    "rugbyunion_world_cup":                 "Rugby World Cup",
    "rugbyunion_women_world_cup":           "Women's Rugby World Cup",
    # ── Aussie Rules ──────────────────────────────────────────────────────────
    "aussierules_afl":                      "AFL",
    "aussierules_aflw":                     "AFLW",
    # ── Hockey ────────────────────────────────────────────────────────────────
    "icehockey_pwhl":                       "PWHL",
    # ── Combat Sports ─────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":               "UFC",
    "boxing_boxing":                        "Boxing",
    # ── Golf ──────────────────────────────────────────────────────────────────
    "golf_pga_tour":                        "PGA Tour",
    "golf_masters_tournament":              "The Masters",
    "golf_pga_championship":                "PGA Championship",
    "golf_us_open":                         "US Open Golf",
    "golf_the_open_championship":           "The Open Championship",
    "golf_lpga":                            "LPGA Tour",
    # ── Tennis ────────────────────────────────────────────────────────────────
    "tennis_atp_french_open":               "French Open",
    "tennis_wta_french_open":               "French Open",
    "tennis_atp_wimbledon":                 "Wimbledon",
    "tennis_wta_wimbledon":                 "Wimbledon",
    "tennis_atp_us_open":                   "US Open Tennis",
    "tennis_wta_us_open":                   "US Open Tennis",
    "tennis_atp_australian_open":           "Australian Open",
    "tennis_wta_australian_open":           "Australian Open",
    # ── Cricket ───────────────────────────────────────────────────────────────
    "cricket_icc_world_cup":                "ICC Cricket World Cup",
    "cricket_ipl":                          "Indian Premier League",
    "cricket_icc_womens_t20_wc":            "ICC Women's T20 World Cup",
}

# In-memory cache: "sport_key:team_name" → team_id
_team_id_cache: dict[str, str] = {}


def _get(path: str) -> dict | None:
    """
    Fetch from TheSportsDB.
    Strategy: direct first (fast, confirmed VPS-friendly).
    If blocked (403/None), automatically retry through Decodo proxy,
    rotating across ports 10001-10010 for each retry.
    """
    import httpx
    from src.apis.base import _HEADERS, _get_direct_client, _get_proxy_client

    url = f"{_BASE}{path}"

    # ── 1. Try direct ─────────────────────────────────────────────────────────
    try:
        r = _get_direct_client().get(url)
        if r.status_code == 200:
            return r.json()
        if r.status_code not in (403, 429, 503):
            return None   # definitive error — don't waste proxy credits
    except Exception:
        pass  # network error → try proxy

    # ── 2. Proxy fallback — rotate across ports 10001-10010 ──────────────────
    try:
        from src.core.config import DECODO_PROXY_URL
        if not DECODO_PROXY_URL:
            return None
        logger.debug("TheSportsDB: direct blocked, retrying via proxy [%s]", path)
        r = _get_proxy_client(DECODO_PROXY_URL).get(url)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("TheSportsDB proxy fallback failed: %s", e)

    return None


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


def find_player_id(player_name: str) -> str | None:
    """Search TheSportsDB for a player by name, return their idPlayer."""
    data = _get(f"/searchplayers.php?p={quote(player_name)}")
    players = (data or {}).get("player") or []
    if not players:
        return None
    return str(players[0].get("idPlayer", ""))


def get_player_stats(player_name: str, sport_key: str = "", opponent: str = "") -> dict:
    """
    Pull player bio and stats from TheSportsDB.
    Returns position, nationality, description and any available stat fields.
    Note: TheSportsDB is not a box-score database — this returns profile/bio data.
    For game logs use Ball Don't Lie (NBA) or sport-specific APIs.
    """
    pid = find_player_id(player_name)
    if not pid:
        return {}
    data = _get(f"/lookupplayer.php?id={pid}")
    players = (data or {}).get("players") or []
    if not players:
        return {}
    p = players[0]
    return {
        "player":       player_name,
        "position":     p.get("strPosition", ""),
        "nationality":  p.get("strNationality", ""),
        "team":         p.get("strTeam", ""),
        "description":  (p.get("strDescriptionEN") or "")[:300],
        "source":       "thesportsdb",
    }


def get_player_recent_events(player_name: str, sport_key: str = "", n: int = 5) -> dict:
    """
    Pull recent team events for the player's team — used as a proxy for player form.
    TheSportsDB does not have per-player box scores, but team form reflects context.
    """
    # Look up which team the player is on, then pull that team's recent form
    profile = get_player_stats(player_name, sport_key)
    team = profile.get("team", "")
    if not team:
        return {}
    form = get_team_form(team, sport_key, n)
    if not form:
        return {}
    return {
        "player":    player_name,
        "team":      team,
        "team_form": form,
        "note":      "team recent results (player-level game log not available via TheSportsDB)",
        "source":    "thesportsdb",
    }


def get_h2h(home_team: str, away_team: str, sport_key: str = "", n: int = 10) -> list:
    """
    Pull head-to-head history between two teams from TheSportsDB.
    Uses /searchevents.php to find past meetings, then resolves scores.
    Returns list of {date, home, away, home_score, away_score, winner}.
    """
    home_id = find_team_id(home_team, sport_key)
    away_id = find_team_id(away_team, sport_key)
    if not home_id or not away_id:
        return []

    # TheSportsDB v1 free tier: search events between two teams
    data = _get(f"/searchevents.php?idHomeTeam={home_id}&idAwayTeam={away_id}")
    events = (data or {}).get("event") or []

    # Also check the reverse fixture (away team hosted home team)
    data2 = _get(f"/searchevents.php?idHomeTeam={away_id}&idAwayTeam={home_id}")
    events += (data2 or {}).get("event") or []

    h2h = []
    for ev in events:
        hs = ev.get("intHomeScore")
        as_ = ev.get("intAwayScore")
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (ValueError, TypeError):
            continue
        home_name = ev.get("strHomeTeam", "")
        away_name = ev.get("strAwayTeam", "")
        winner = home_name if hs > as_ else (away_name if as_ > hs else "Draw")
        h2h.append({
            "date":       ev.get("dateEvent", ""),
            "home":       home_name,
            "away":       away_name,
            "home_score": hs,
            "away_score": as_,
            "winner":     winner,
        })

    # Sort newest first, cap at n
    h2h.sort(key=lambda x: x["date"], reverse=True)
    return h2h[:n]


def enrich_game_context(sport_key: str, home_team: str, away_team: str) -> dict:
    """
    Pull recent form for both teams + H2H history from TheSportsDB.
    Returns dict ready for injection into data_hub context.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home": (get_team_form, (home_team, sport_key, 10)),
        "away": (get_team_form, (away_team, sport_key, 10)),
        "h2h":  (get_h2h,      (home_team, away_team, sport_key, 10)),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        for future in _as_completed(futures, timeout=20):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                pass

    home_form = results.get("home", {})
    away_form = results.get("away", {})
    h2h       = results.get("h2h", [])

    if not home_form and not away_form:
        return {}

    return {
        "available":  True,
        "home_form":  home_form,
        "away_form":  away_form,
        "h2h":        h2h,
        "source":     "thesportsdb",
    }
