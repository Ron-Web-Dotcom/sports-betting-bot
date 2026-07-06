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
import threading
import time
from datetime import datetime
from urllib.parse import quote
from src.core.timezone import et_naive, ET

logger = logging.getLogger(__name__)

_BASE = "https://api.sofascore.com/api/v1"

# ── Circuit breaker — trip after 3 consecutive 403s, reset after 30 min ────────
_cb_lock         = threading.Lock()
_cb_failures     = 0
_cb_tripped_at   = 0.0
_CB_THRESHOLD    = 5      # trip after this many consecutive 403s (wider pool = more tolerance)
_CB_RESET_SECS   = 900    # try again after 15 minutes (was 30)

def _cb_is_open() -> bool:
    """Return True if circuit is tripped (Sofascore is blocked)."""
    global _cb_failures
    with _cb_lock:
        if _cb_failures >= _CB_THRESHOLD:
            if time.monotonic() - _cb_tripped_at < _CB_RESET_SECS:
                return True
            # Cooldown elapsed — reset counter so probe needs N failures to re-trip
            _cb_failures = 0
            return False
        return False

def _cb_record_failure():
    global _cb_failures, _cb_tripped_at
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_tripped_at = time.monotonic()
            logger.warning("Sofascore circuit breaker TRIPPED — skipping all calls for %ds", _CB_RESET_SECS)

def _cb_record_success():
    global _cb_failures
    with _cb_lock:
        _cb_failures = 0

# ── Sofascore-specific proxy port pool ───────────────────────────────────────
# Decodo sticky residential endpoints: ports 10001-10050 (50 unique IPs).
# General sources use 10001-10010.  Sofascore gets its own wider slice:
#   Primary:  10021-10050  (30 fresh IPs, never used for Sofascore before)
#   Fallback: 10011-10020  (original 10 — may be temporarily banned)
# Env override: SOFASCORE_PROXY_PORTS=10021,10025,10030 (comma-separated ints)
import itertools as _itertools
import os as _os

def _build_sf_ports() -> list[int]:
    env = _os.getenv("SOFASCORE_PROXY_PORTS", "")
    if env:
        try:
            return [int(p.strip()) for p in env.split(",") if p.strip()]
        except ValueError:
            pass
    return list(range(10021, 10051))   # 30 fresh ports by default

_SF_PORTS      = _build_sf_ports()
_sf_port_cycle = _itertools.cycle(_SF_PORTS)
_sf_port_lock  = threading.Lock()

def _next_sf_port() -> int:
    with _sf_port_lock:
        return next(_sf_port_cycle)

# ── Decodo health tracker — auto-switch back when Decodo recovers ─────────────
_decodo_ok        = True   # assume healthy at start
_decodo_fail_at   = 0.0
_DECODO_RETRY_SEC = 1800   # re-probe Decodo every 30 min after a failure
_decodo_lock      = threading.Lock()

def _decodo_is_healthy() -> bool:
    global _decodo_ok, _decodo_fail_at
    with _decodo_lock:
        if _decodo_ok:
            return True
        # Retry after cooldown
        if time.monotonic() - _decodo_fail_at >= _DECODO_RETRY_SEC:
            _decodo_ok = True
            logger.info("Sofascore: re-probing Decodo proxy after cooldown")
            return True
        return False

def _decodo_mark_failed():
    global _decodo_ok, _decodo_fail_at
    with _decodo_lock:
        _decodo_ok      = False
        _decodo_fail_at = time.monotonic()

def _decodo_mark_ok():
    global _decodo_ok
    with _decodo_lock:
        _decodo_ok = True


def _get_decodo_client():
    import httpx
    from src.core.config import DECODO_PROXY_URL
    port = _next_sf_port()
    return httpx.Client(
        timeout=httpx.Timeout(connect=8.0, read=25.0, write=5.0, pool=5.0),
        follow_redirects=True,
        verify=False,
        proxy=f"{DECODO_PROXY_URL}:{port}",
    )


def _get_sf_client():
    """
    Returns Decodo client. Used only when Decodo is healthy.
    ScraperAPI fallback is handled directly in _get().
    """
    return _get_decodo_client()

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
    "soccer_uefa_nations_league":            "football",
    # ── Soccer — Europe Other ─────────────────────────────────────────────────
    "soccer_belgium_first_div":              "football",
    "soccer_greece_super_league":            "football",
    "soccer_denmark_superliga":              "football",
    "soccer_sweden_allsvenskan":             "football",
    "soccer_norway_eliteserien":             "football",
    "soccer_finland_veikkausliiga":          "football",
    "soccer_austria_bundesliga":             "football",
    "soccer_swiss_superleague":              "football",
    "soccer_czech_liga":                     "football",
    "soccer_poland_ekstraklasa":             "football",
    "soccer_romania_liga_1":                 "football",
    "soccer_croatia_hnl":                    "football",
    # ── Soccer — South America ────────────────────────────────────────────────
    "soccer_chile_primera_division":         "football",
    "soccer_colombia_primera_a":             "football",
    "soccer_ecuador_liga_pro":               "football",
    "soccer_uruguay_primera_division":       "football",
    "soccer_peru_primera_division":          "football",
    "soccer_venezuela_primera_liga":         "football",
    # ── Soccer — Asia / Oceania / Middle East ─────────────────────────────────
    "soccer_japan_j_league":                 "football",
    "soccer_south_korea_kleague1":           "football",
    "soccer_china_superleague":              "football",
    "soccer_saudi_arabia_premier_league":    "football",
    "soccer_australia_aleague":              "football",
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
    "golf_dp_world_tour":                    "golf",
    # ── Motorsport ───────────────────────────────────────────────────────────
    "motorsport_formula_1":                  "formula-1",
    "motorsport_indycar":                    "motorsport",
    "motorsport_nascar_cup_series":          "motorsport",
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
    "tennis_atp_madrid":                     "tennis",
    "tennis_atp_rome":                       "tennis",
    "tennis_atp_miami":                      "tennis",
    "tennis_atp_indian_wells":               "tennis",
    "tennis_atp_toronto":                    "tennis",
    "tennis_atp_cincinnati":                 "tennis",
    # ── Women's Hockey ────────────────────────────────────────────────────────
    "icehockey_pwhl":                        "ice-hockey",
    # ── Aussie Rules (Men's + Women's) ───────────────────────────────────────
    "aussierules_afl":                       "australian-football",
    "aussierules_aflw":                      "australian-football",
    # ── Rugby (Men's + Women's) ───────────────────────────────────────────────
    "rugbyunion_world_cup":                  "rugby",
    "rugbyunion_women_world_cup":            "rugby",
    "rugbyunion_super_rugby":                "rugby",
    "rugbyunion_premiership":                "rugby",
    "rugbyunion_top14":                      "rugby",
    "rugbyunion_united_rugby_championship":  "rugby",
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
    "cricket_t20_blast":                     "cricket",
    # ── Table Tennis ──────────────────────────────────────────────────────────
    "tabletennis_wtt":                       "table-tennis",
    "tabletennis_ittf":                      "table-tennis",
    # ── Volleyball ────────────────────────────────────────────────────────────
    "volleyball_wvl":                        "volleyball",
    "volleyball_men_wc":                     "volleyball",
    "volleyball_women_wc":                   "volleyball",
    # ── Handball ──────────────────────────────────────────────────────────────
    "handball_world_championship":           "handball",
    "handball_ehf_champions_league":         "handball",
    # ── Badminton ─────────────────────────────────────────────────────────────
    "badminton_bwf_world_tour":              "badminton",
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker_world_championship":            "snooker",
    # ── Esports ───────────────────────────────────────────────────────────────
    "esports_lol":                           "esports",
    "esports_csgo":                          "esports",
    "esports_dota2":                         "esports",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept":              "application/json, text/plain, */*",
    "Accept-Language":     "en-US,en;q=0.9",
    "Accept-Encoding":     "gzip, deflate, br",
    "Referer":             "https://www.sofascore.com/",
    "Origin":              "https://www.sofascore.com",
    "Cache-Control":       "no-cache",
    "Pragma":              "no-cache",
    "sec-ch-ua":           '"Google Chrome";v="137", "Chromium";v="137", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile":    "?0",
    "sec-ch-ua-platform":  '"Windows"',
    "Sec-Fetch-Dest":      "empty",
    "Sec-Fetch-Mode":      "cors",
    "Sec-Fetch-Site":      "same-origin",
    # Session cookie — set by sofascore.com on first visit; helps bypass bot detection.
    # Override via SOFASCORE_SESSION env var if the default gets blocked.
    "Cookie":              __import__('os').getenv("SOFASCORE_SESSION", "TS_session=1; sofascore_gdpr_consent=1"),
}


def _slug(sport_key: str) -> str | None:
    return SPORT_MAP.get(sport_key)


def _scraperapi_get(path: str, api_key: str) -> dict | list | None:
    """Make a Sofascore request via ScraperAPI REST endpoint."""
    import httpx
    target_url = f"{_BASE}{path}"
    try:
        r = httpx.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": target_url, "keep_headers": "true"},
            headers=_HEADERS,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0),
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            logger.debug("Sofascore 404 via ScraperAPI [%s] (no games)", path)
        else:
            logger.warning("Sofascore %s via ScraperAPI [%s]", r.status_code, path)
        return None
    except Exception as e:
        logger.warning("Sofascore ScraperAPI request failed [%s]: %s", path, e)
        return None


def _get(path: str) -> dict | list | None:
    """
    Sofascore request with auto-routing:
      1. Decodo proxy (primary) — fast, unlimited
      2. ScraperAPI (backup)    — kicks in when Decodo is blocked
      3. Auto-recovery          — re-probes Decodo every 30 min; switches back when healthy

    Circuit breaker still applies — trips after 5 consecutive failures from ANY source.
    """
    if _cb_is_open():
        return None

    import os
    import re
    # Import config first — triggers load_dotenv() so env vars are available
    from src.core.config import DECODO_PROXY_URL
    target_url = f"{_BASE}{path}"
    sf_proxy   = os.getenv("SOFASCORE_PROXY_URL", "")
    scraper_key = ""
    if sf_proxy and "scraperapi.com" in sf_proxy:
        m = re.search(r"scraperapi:([^@]+)@", sf_proxy)
        scraper_key = m.group(1) if m else ""

    # ── Try Decodo first (if healthy and configured) ──────────────────────────
    if DECODO_PROXY_URL and _decodo_is_healthy():
        try:
            client = _get_decodo_client()
            r = client.get(target_url, headers=_HEADERS)
            if r.status_code == 200:
                _cb_record_success()
                _decodo_mark_ok()
                return r.json()
            if r.status_code in (403, 429):
                logger.warning("Sofascore: Decodo blocked (%s) — switching to ScraperAPI", r.status_code)
                _decodo_mark_failed()
                # fall through to ScraperAPI below
            elif r.status_code == 404:
                logger.debug("Sofascore 404 via Decodo [%s] (no games)", path)
                return None
            else:
                logger.warning("Sofascore HTTP %s via Decodo [%s]", r.status_code, path)
                _cb_record_failure()
                # fall through to ScraperAPI for unexpected status codes
        except Exception as e:
            logger.warning("Sofascore Decodo request failed [%s]: %s", path, e)
            _decodo_mark_failed()
            # fall through to ScraperAPI below

    # ── ScraperAPI fallback ───────────────────────────────────────────────────
    if scraper_key:
        result = _scraperapi_get(path, scraper_key)
        if result is not None:
            _cb_record_success()
        else:
            _cb_record_failure()
        return result

    # No working proxy — config issue, not a Sofascore block; don't trip circuit breaker
    logger.warning("Sofascore: no working proxy configured for [%s]", path)
    return None


# ── Scheduled events ──────────────────────────────────────────────────────────

def get_scheduled_events(sport_key: str, date: str | None = None) -> list[dict]:
    """Return matches for a single sport_key on a given date."""
    slug = _slug(sport_key)
    if not slug:
        return []
    date = date or et_naive().strftime("%Y-%m-%d")
    data = _get(f"/sport/{slug}/scheduled-events/{date}")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    return [_normalise_event(e, sport_key) for e in events]


def get_all_scheduled_events(date: str | None = None) -> list[dict]:
    """
    Return ALL of today's events (scheduled + live) across every sport worldwide.

    Makes 30 requests (15 slugs × 2 endpoints: scheduled + live) instead of
    110+ per-sport_key calls. Covers every sport Sofascore tracks globally:
    soccer (World Cup, all leagues), NFL, NBA, MLB, NHL, tennis, cricket,
    rugby, MMA, motorsport, golf, aussie rules, and more.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    date = date or et_naive().strftime("%Y-%m-%d")

    # slug → [sport_keys] reverse map
    slug_to_keys: dict[str, list[str]] = {}
    for sk, slug in SPORT_MAP.items():
        slug_to_keys.setdefault(slug, []).append(sk)

    all_events: list[dict] = []
    seen_ids: set[str] = set()

    def _fetch(slug: str, endpoint: str) -> list[dict]:
        if _cb_is_open():
            return []
        data = _get(endpoint)
        if not data:
            return []
        raw  = data if isinstance(data, list) else data.get("events", [])
        keys = slug_to_keys.get(slug, [slug])
        return [_normalise_event(e, keys[0]) for e in raw]

    # Build task list: scheduled events + live events per slug
    tasks: list[tuple[str, str]] = []
    for slug in slug_to_keys:
        tasks.append((slug, f"/sport/{slug}/scheduled-events/{date}"))
        tasks.append((slug, f"/sport/{slug}/events/live"))

    # ScraperAPI is slower than direct proxy — use 4 workers and generous per-future timeout
    import os as _os
    using_scraper = bool(_os.getenv("SOFASCORE_PROXY_URL", ""))
    max_workers   = 3 if using_scraper else 6
    fut_timeout   = 45 if using_scraper else 20

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, slug, endpoint): (slug, endpoint) for slug, endpoint in tasks}
        for fut in as_completed(futures, timeout=fut_timeout * len(tasks)):
            slug, endpoint = futures[fut]
            try:
                for ev in fut.result():
                    eid = ev.get("id", "")
                    if eid and eid in seen_ids:
                        continue
                    if eid:
                        seen_ids.add(eid)
                    all_events.append(ev)
            except Exception as e:
                logger.warning("Sofascore batch [%s %s]: %s", slug, endpoint, e)

    scheduled = sum(1 for e in all_events if not e.get("status_type", "").startswith("inprogress"))
    live      = len(all_events) - scheduled
    logger.info("Sofascore batch scan: %d scheduled + %d live = %d total events across %d sports",
                scheduled, live, len(all_events), len(slug_to_keys))
    return all_events


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


def get_event_result(event_id: str) -> dict | None:
    """
    Fetch the current/final result for a specific Sofascore event by ID.
    Returns normalised dict with home_score, away_score, status_type, winner.
    Returns None if the event can't be fetched.
    """
    data = _get(f"/event/{event_id}")
    if not data:
        return None
    ev = data.get("event", data) if isinstance(data, dict) else None
    if not ev:
        return None
    status = ev.get("status", {})
    hs = (ev.get("homeScore") or {}).get("current")
    as_ = (ev.get("awayScore") or {}).get("current")
    status_type = status.get("type", "")
    winner: str
    if hs is not None and as_ is not None:
        if hs > as_:
            winner = (ev.get("homeTeam") or {}).get("name", "home")
        elif as_ > hs:
            winner = (ev.get("awayTeam") or {}).get("name", "away")
        else:
            winner = "draw"
    else:
        winner = "unknown"
    return {
        "event_id":    str(event_id),
        "home_team":   (ev.get("homeTeam") or {}).get("name", ""),
        "away_team":   (ev.get("awayTeam") or {}).get("name", ""),
        "home_score":  hs,
        "away_score":  as_,
        "status_type": status_type,          # "notstarted" / "inprogress" / "finished"
        "status_desc": status.get("description", ""),
        "winner":      winner,
        "is_live":     status_type == "inprogress",
        "is_finished": status_type == "finished",
    }


def find_event_by_teams(home_team: str, away_team: str, date_str: str | None = None,
                        sport_key: str | None = None) -> dict | None:
    """
    Search today's (or date_str's) Sofascore events for a match between these two teams.
    Returns the normalised event dict if found, else None.
    Uses cached scan data from Redis when available.
    """
    import json as _json
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        cached = r.get("sofascore:today_events")
        if cached:
            events = _json.loads(cached)
        else:
            events = get_all_scheduled_events(date_str)
    except Exception:
        events = get_all_scheduled_events(date_str)

    home_n = home_team.lower().strip()
    away_n = away_team.lower().strip()
    for ev in events:
        eh = (ev.get("home_team") or "").lower()
        ea = (ev.get("away_team") or "").lower()
        home_match = home_n in eh or eh in home_n
        away_match = away_n in ea or ea in away_n
        if home_match and away_match:
            if sport_key and ev.get("sport") and ev["sport"] != sport_key:
                continue
            return ev
    return None


def _normalise_event(e: dict, sport_key: str) -> dict:
    home = e.get("homeTeam", {})
    away = e.get("awayTeam", {})
    score = e.get("homeScore", {}), e.get("awayScore", {})
    status = e.get("status", {})
    # Capture team country — used to match Kalshi country-name subtitles
    # e.g. Kalshi "Germany vs Ecuador" → Sofascore "Bayern Munich" (country: Germany)
    home_country = (home.get("country") or {}).get("name", "")
    away_country = (away.get("country") or {}).get("name", "") or ""
    tournament = e.get("tournament", {})
    season     = e.get("season", {})
    return {
        "id":              str(e.get("id", "")),
        "sport":           sport_key,
        "home_team":       home.get("name", ""),
        "home_team_id":    str(home.get("id", "")),
        "home_country":    home_country,
        "away_team":       away.get("name", ""),
        "away_team_id":    str(away.get("id", "")),
        "away_country":    away_country,
        "home_score":      score[0].get("current"),
        "away_score":      score[1].get("current"),
        "status":          status.get("description", ""),
        "status_type":     status.get("type", ""),
        "commence_time":   _epoch_to_iso(e.get("startTimestamp")),
        "tournament":      tournament.get("name", ""),
        "tournament_id":   str(tournament.get("uniqueTournament", {}).get("id", "") or
                              tournament.get("id", "")),
        "season":          season.get("name", ""),
        "season_id":       str(season.get("id", "")),
        "source":          "sofascore",
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
            try:
                result = fut.result()
                if result:
                    active.add(result)
            except Exception as _e:
                logger.warning("Sofascore active-sport check failed: %s", _e)

    logger.info("Sofascore active sports today: %s", sorted(active))
    return active


def _epoch_to_iso(ts: int | None) -> str:
    if ts is None:
        return ""
    try:
        # Naive ET string — scan_todays_games treats naive strings as ET
        return datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%dT%H:%M:%S")
    except (OSError, ValueError):
        return ""


# ── Odds ──────────────────────────────────────────────────────────────────────

def get_event_odds(event_id: str) -> dict:
    """
    Return pre-match 1X2 odds for a given Sofascore event.
    Sofascore aggregates odds from bookmakers (Bet365 = bookmaker 1).
    Returns implied probabilities + American odds for home/draw/away.
    """
    data = _get(f"/event/{event_id}/odds/1/all")
    if not data:
        return {}
    try:
        markets = data.get("markets", []) if isinstance(data, dict) else []
        for market in markets:
            if market.get("marketName", "").lower() in ("1x2", "match winner", "moneyline"):
                choices = market.get("choices", [])
                result = {}
                for c in choices:
                    name = c.get("name", "").upper()
                    odd  = float(c.get("odd") or c.get("fractionalValue") or 0)
                    if not odd:
                        continue
                    # Convert decimal to American
                    if odd >= 2.0:
                        american = f"+{int((odd - 1) * 100)}"
                    elif odd > 1.0:
                        american = str(int(-100 / (odd - 1)))
                    else:
                        continue  # odd == 1.0 means 100% probability — invalid, skip
                    implied = round(1 / odd, 4)
                    if name in ("1", "HOME"):
                        result["home_odds"]    = american
                        result["home_implied"] = implied
                    elif name in ("X", "DRAW"):
                        result["draw_odds"]    = american
                        result["draw_implied"] = implied
                    elif name in ("2", "AWAY"):
                        result["away_odds"]    = american
                        result["away_implied"] = implied
                if result:
                    result["source"] = "sofascore"
                    return result
    except Exception as e:
        logger.warning("get_event_odds(%s) parse error: %s", event_id, e)
    return {}


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
                "home"    if (hs is not None and as_ is not None and hs > as_) else
                "away"    if (hs is not None and as_ is not None and as_ > hs) else
                "unknown" if (hs is None or as_ is None) else
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
    stats = data.get("statistics", data) if isinstance(data, dict) else data
    return {"player_id": player_id, "season_id": season_id,
            "stats": stats, "source": "sofascore"}


def search_player(name: str, sport_key: str) -> list[dict]:
    """Search for a player by name across all sports."""
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


def get_team_standings_for_event(event: dict) -> dict:
    """
    Given a normalised event dict (with tournament_id + season_id), return
    standings rows for the home and away team, including their league position,
    points, wins, losses, and goals. Returns {} if standings unavailable.
    """
    tid = event.get("tournament_id", "")
    sid = event.get("season_id", "")
    if not tid or not sid:
        return {}
    try:
        rows = get_standings(tid, sid)
    except Exception:
        return {}
    if not rows:
        return {}

    home_name = (event.get("home_team") or "").lower()
    away_name = (event.get("away_team") or "").lower()
    home_row  = next((r for r in rows if home_name in r["team"].lower() or r["team"].lower() in home_name), None)
    away_row  = next((r for r in rows if away_name in r["team"].lower() or r["team"].lower() in away_name), None)

    result: dict = {"total_teams": len(rows)}
    if home_row:
        result["home_position"] = home_row.get("position")
        result["home_points"]   = home_row.get("points")
        result["home_played"]   = home_row.get("played")
        result["home_w_d_l"]    = f"{home_row.get('wins',0)}-{home_row.get('draws',0)}-{home_row.get('losses',0)}"
        result["home_gf_ga"]    = f"{home_row.get('goals_for',0)}-{home_row.get('goals_against',0)}"
    if away_row:
        result["away_position"] = away_row.get("position")
        result["away_points"]   = away_row.get("points")
        result["away_played"]   = away_row.get("played")
        result["away_w_d_l"]    = f"{away_row.get('wins',0)}-{away_row.get('draws',0)}-{away_row.get('losses',0)}"
        result["away_gf_ga"]    = f"{away_row.get('goals_for',0)}-{away_row.get('goals_against',0)}"
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
    h2h history, form, standings, and last 5 events per team.

    Matches by team name substring — best-effort.
    """
    date = game_time[:10] if game_time else et_naive().strftime("%Y-%m-%d")
    events = get_scheduled_events(sport_key, date)

    matched = None
    for ev in events:
        ev_home = (ev.get("home_team") or "").lower()
        ev_away = (ev.get("away_team") or "").lower()
        if ((home_team.lower() in ev_home or ev_home in home_team.lower()) and
                (away_team.lower() in ev_away or ev_away in away_team.lower())):
            matched = ev
            break

    if not matched:
        return {"available": False, "sport": sport_key, "source": "sofascore"}

    eid = matched.get("id")
    if not eid:
        return {"available": False, "sport": sport_key, "source": "sofascore"}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_h2h       = pool.submit(get_h2h, eid)
        f_form      = pool.submit(get_team_form, eid)
        f_standings = pool.submit(get_team_standings_for_event, matched)
        f_home_last = pool.submit(get_team_last_events, matched.get("home_team_id", ""))
        f_away_last = pool.submit(get_team_last_events, matched.get("away_team_id", ""))
        f_stats     = pool.submit(get_event_statistics, eid)
        # pool.__exit__ waits for all futures — safe to call .result() below

    def _get_result(f, default):
        try:
            return f.result()
        except Exception:
            return default

    context = {
        "available":    True,
        "event":        matched,
        "tournament":   matched.get("tournament", ""),
        "season":       matched.get("season", ""),
        "h2h":          _get_result(f_h2h, []),
        "form":         _get_result(f_form, {}),
        "standings":    _get_result(f_standings, {}),
        "home_last5":   _get_result(f_home_last, []),
        "away_last5":   _get_result(f_away_last, []),
        "event_stats":  _get_result(f_stats, {}),  # possession, shots, corners etc.
        "source":       "sofascore",
    }
    return context
