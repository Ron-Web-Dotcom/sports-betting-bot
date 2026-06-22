"""
RapidAPI fallback odds adapter.
Used when Odds API credits are exhausted. Auto-detected via 401 response.
Rotates across 3 subscribed APIs to balance request limits.
Returns data normalized to the same format as the Odds API adapter so
the rest of the codebase needs zero changes.

Normalized event format (matches odds_engine expectations):
{
    "id": str,
    "sport_key": str,
    "home_team": str,
    "away_team": str,
    "commence_time": str,  # ISO8601
    "bookmakers": [
        {
            "key": str,  # e.g. "draftkings"
            "markets": [
                {
                    "key": str,  # "h2h", "spreads", "totals"
                    "outcomes": [
                        {"name": str, "price": int}  # American odds
                    ]
                }
            ]
        }
    ]
}
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from src.apis.base import get_json

logger = logging.getLogger(__name__)

# ── RapidAPI hosts ─────────────────────────────────────────────────────────────
_SPORTSBOOK_HOST  = "sportsbook-api2.p.rapidapi.com"
_ODDSPAPI_HOST    = "odds-api1.p.rapidapi.com"
_LIVESCORE_HOST   = "free-livescore-api.p.rapidapi.com"

_RAPIDAPI_BASE    = "https://{host}"

# ── Sport routing ──────────────────────────────────────────────────────────────
# Sportsbook API → US + major sports
_SPORTSBOOK_SPORT_MAP: dict[str, str] = {
    # US Major
    "americanfootball_nfl":              "NFL",
    "americanfootball_ncaaf":            "NCAAF",
    "basketball_nba":                    "NBA",
    "basketball_wnba":                   "WNBA",
    "basketball_ncaab":                  "NCAAB",
    "basketball_wncaab":                 "NCAAB",
    "baseball_mlb":                      "MLB",
    "icehockey_nhl":                     "NHL",
    "icehockey_pwhl":                    "PWHL",
    # Soccer via Sportsbook API (fallback for those not in OddsPapi)
    "soccer_usa_mls":                    "MLS",
    "soccer_usa_nwsl":                   "NWSL",
    "soccer_fifa_club_world_cup":        "FIFA Club World Cup",
    "soccer_epl":                        "English Premier League",
    "soccer_germany_bundesliga":         "German Bundesliga",
    "soccer_spain_la_liga":              "Spanish La Liga",
    "soccer_italy_serie_a":              "Italian Serie A",
    "soccer_france_ligue_one":           "French Ligue 1",
    # Combat
    "mma_mixed_martial_arts":            "MMA",
    "boxing_boxing":                     "Boxing",
    # Golf
    "golf_pga_tour":                     "Golf",
    "golf_lpga":                         "Golf",
    "golf_masters_tournament_winner":    "Golf",
    "golf_pga_championship_winner":      "Golf",
    "golf_us_open_winner":               "Golf",
    "golf_the_open_championship_winner": "Golf",
    "golf_dp_world_tour":                "Golf",
    # Aussie Rules
    "aussierules_afl":                   "AFL",
    "aussierules_aflw":                  "AFL",
    # Rugby
    "rugbyleague_nrl":                   "NRL",
    "rugbyleague_nrl_state_of_origin":   "NRL",
    "rugbyunion_super_rugby":            "Rugby Union",
    "rugbyunion_premiership":            "Rugby Union",
    "rugbyunion_top14":                  "Rugby Union",
    "rugbyunion_united_rugby_championship": "Rugby Union",
    "rugbyunion_world_cup":              "Rugby Union",
    # Tennis
    "tennis_atp_wimbledon":              "Tennis",
    "tennis_wta_wimbledon":              "Tennis",
    "tennis_atp_us_open":                "Tennis",
    "tennis_wta_us_open":                "Tennis",
    "tennis_atp_australian_open":        "Tennis",
    "tennis_wta_aus_open_singles":       "Tennis",
    "tennis_atp_french_open":            "Tennis",
    "tennis_wta_french_open":            "Tennis",
    "tennis_atp_queens_club_champ":      "Tennis",
    "tennis_atp_halle_open":             "Tennis",
    "tennis_wta_german_open":            "Tennis",
    "tennis_atp_toronto":                "Tennis",
    "tennis_wta_toronto":                "Tennis",
    "tennis_atp_cincinnati":             "Tennis",
    "tennis_wta_cincinnati":             "Tennis",
    "tennis_atp_madrid":                 "Tennis",
    "tennis_atp_rome":                   "Tennis",
    "tennis_atp_miami":                  "Tennis",
    "tennis_atp_indian_wells":           "Tennis",
    # Cricket
    "cricket_international_t20":         "Cricket",
    "cricket_odi":                       "Cricket",
    "cricket_test_match":                "Cricket",
    "cricket_ipl":                       "Cricket",
    "cricket_t20_blast":                 "Cricket",
    "cricket_t20_world_cup_womens":      "Cricket",
    # Motor Racing
    "motorsport_formula_1":              "Formula 1",
    "motorsport_indycar":                "IndyCar",
    "motorsport_nascar_cup_series":      "NASCAR",
}

# Soccer → OddsPapi (sport_id values known from their /sports endpoint)
_ODDSPAPI_SPORT_MAP: dict[str, str] = {
    "soccer_epl":                            "1",
    "soccer_spain_la_liga":                  "2",
    "soccer_germany_bundesliga":             "3",
    "soccer_italy_serie_a":                  "4",
    "soccer_france_ligue_one":               "5",
    "soccer_usa_mls":                        "6",
    "soccer_netherlands_eredivisie":         "7",
    "soccer_portugal_primeira_liga":         "8",
    "soccer_mexico_ligamx":                  "9",
    "soccer_argentina_primera_division":     "10",
    "soccer_brazil_campeonato":              "11",
    "soccer_turkey_super_league":            "12",
    "soccer_spl":                            "13",
    "soccer_uefa_champs_league":             "14",
    "soccer_uefa_europa_league":             "15",
    "soccer_uefa_europa_conference_league":  "16",
    "soccer_fifa_world_cup":                 "17",
    "soccer_fifa_club_world_cup":            "18",
    "soccer_conmebol_copa_america":          "19",
    "soccer_conmebol_copa_libertadores":     "20",
    "soccer_conmebol_copa_sudamericana":     "21",
    "soccer_africa_cup_of_nations":          "22",
    "soccer_usa_nwsl":                       "23",
    "soccer_fifa_womens_world_cup":          "24",
    "soccer_uefa_womens_champs_league":      "25",
    "soccer_england_wsl":                    "26",
    "soccer_germany_frauen_bundesliga":      "27",
    "soccer_spain_liga_f":                   "28",
    "soccer_france_d1_feminine":             "29",
    "soccer_italy_serie_a_feminine":         "30",
    "soccer_chile_primera_division":         "31",
    "soccer_colombia_primera_a":             "32",
    "soccer_ecuador_liga_pro":               "33",
    "soccer_uruguay_primera_division":       "34",
    "soccer_peru_primera_division":          "35",
    "soccer_venezuela_primera_liga":         "36",
    "soccer_japan_j_league":                 "37",
    "soccer_south_korea_kleague1":           "38",
    "soccer_china_superleague":              "39",
    "soccer_saudi_arabia_premier_league":    "40",
    "soccer_australia_aleague":              "41",
    "soccer_denmark_superliga":              "42",
    "soccer_sweden_allsvenskan":             "43",
    "soccer_norway_eliteserien":             "44",
    "soccer_finland_veikkausliiga":          "45",
    "soccer_austria_bundesliga":             "46",
    "soccer_swiss_superleague":              "47",
    "soccer_czech_liga":                     "48",
    "soccer_poland_ekstraklasa":             "49",
    "soccer_romania_liga_1":                 "50",
    "soccer_croatia_hnl":                    "51",
    "soccer_belgium_first_div":              "52",
    "soccer_greece_super_league":            "53",
    "soccer_uefa_nations_league":            "54",
}

# ── Book key normalisation ─────────────────────────────────────────────────────
_BOOK_KEY_MAP: dict[str, str] = {
    "draftkings":  "draftkings",
    "draft kings": "draftkings",
    "fanduel":     "fanduel",
    "fan duel":    "fanduel",
    "betmgm":      "betmgm",
    "bet mgm":     "betmgm",
    "caesars":     "caesars",
    "espn bet":    "espnbet",
    "espnbet":     "espnbet",
    "bovada":      "bovada",
}


def _book_key(raw: str) -> str:
    return _BOOK_KEY_MAP.get(raw.lower().strip(), raw.lower().replace(" ", ""))


def _rapidapi_headers() -> dict[str, str]:
    key = os.getenv("RAPIDAPI_KEY", "")
    return {
        "x-rapidapi-key": key,
        "x-rapidapi-host": "",  # overridden per call
    }


def _get(host: str, path: str, params: dict | None = None) -> dict | list | None:
    url = f"https://{host}{path}"
    headers = _rapidapi_headers()
    headers["x-rapidapi-host"] = host
    return get_json(url, params=params, headers=headers)


# ── Redis helpers ──────────────────────────────────────────────────────────────
_CACHE_TTL = 25 * 60  # 25 minutes


def _redis():
    from src.core.config import REDIS_URL
    import redis
    return redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


def _cache_get(key: str) -> list | None:
    try:
        r = _redis()
        val = r.get(key)
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


def _cache_set(key: str, data: list) -> None:
    try:
        r = _redis()
        r.setex(key, _CACHE_TTL, json.dumps(data))
    except Exception:
        pass


# ── Normalisation helpers ──────────────────────────────────────────────────────

def _make_event_id(sport_key: str, home: str, away: str, commence_time: str) -> str:
    """Generate a stable event ID when the source doesn't provide one."""
    raw = f"{sport_key}:{home}:{away}:{commence_time}"
    return hashlib.md5(raw.encode()).hexdigest()


def _normalise_ts(raw: str | None) -> str:
    """Coerce various timestamp formats to ISO8601 UTC string."""
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    # Already looks like ISO
    if "T" in str(raw):
        return str(raw)
    # Unix timestamp
    try:
        ts = float(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return str(raw)


# ── Sportsbook API normalisation ───────────────────────────────────────────────

def _parse_sportsbook_response(data: list | dict, sport_key: str) -> list[dict]:
    """
    Sportsbook API GET /v0/advantages/ response → normalised events.

    The response is an array of advantage objects, each representing one
    odds line for a matchup at one book.  We group by matchup to produce
    one event dict per game.
    """
    if not data:
        return []

    # Support both list and wrapped {"data": [...]} shapes
    rows: list = data if isinstance(data, list) else data.get("data", []) or data.get("advantages", [])

    events: dict[str, dict] = {}  # keyed by stable event id

    for row in rows:
        home = row.get("home_team") or row.get("homeTeam") or ""
        away = row.get("away_team") or row.get("awayTeam") or row.get("visitor_team") or row.get("visitorTeam") or ""
        ts   = row.get("commence_time") or row.get("start_time") or row.get("startTime") or row.get("game_time") or ""
        ts   = _normalise_ts(str(ts) if ts else None)

        eid = row.get("id") or row.get("game_id") or _make_event_id(sport_key, home, away, ts)

        if eid not in events:
            events[eid] = {
                "id":            str(eid),
                "sport_key":     sport_key,
                "home_team":     home,
                "away_team":     away,
                "commence_time": ts,
                "bookmakers":    [],
            }

        # Extract book/odds info
        book_raw  = row.get("book") or row.get("sportsbook") or row.get("bookmaker") or ""
        book      = _book_key(book_raw)

        # Find or create bookmaker entry
        bk_entry = next((b for b in events[eid]["bookmakers"] if b["key"] == book), None)
        if not bk_entry:
            bk_entry = {"key": book, "markets": []}
            events[eid]["bookmakers"].append(bk_entry)

        # h2h market
        home_odds = row.get("home_odds") or row.get("homeOdds") or row.get("home_moneyline")
        away_odds = row.get("away_odds") or row.get("awayOdds") or row.get("away_moneyline")
        draw_odds = row.get("draw_odds") or row.get("drawOdds")

        if home_odds or away_odds:
            h2h_outcomes = []
            if away_odds:
                try:
                    h2h_outcomes.append({"name": away, "price": int(float(away_odds))})
                except (TypeError, ValueError):
                    pass
            if home_odds:
                try:
                    h2h_outcomes.append({"name": home, "price": int(float(home_odds))})
                except (TypeError, ValueError):
                    pass
            if draw_odds:
                try:
                    h2h_outcomes.append({"name": "Draw", "price": int(float(draw_odds))})
                except (TypeError, ValueError):
                    pass
            if h2h_outcomes:
                _upsert_market(bk_entry["markets"], "h2h", h2h_outcomes)

        # spread market
        home_spread = row.get("home_spread") or row.get("homeSpread") or row.get("spread")
        home_spread_odds = row.get("home_spread_odds") or row.get("homeSpreadOdds")
        away_spread_odds = row.get("away_spread_odds") or row.get("awaySpreadOdds")
        if home_spread and (home_spread_odds or away_spread_odds):
            spread_outcomes = []
            try:
                pt = float(home_spread)
                if away_spread_odds:
                    spread_outcomes.append({"name": away, "price": int(float(away_spread_odds)), "point": -pt})
                if home_spread_odds:
                    spread_outcomes.append({"name": home, "price": int(float(home_spread_odds)), "point": pt})
            except (TypeError, ValueError):
                pass
            if spread_outcomes:
                _upsert_market(bk_entry["markets"], "spreads", spread_outcomes)

        # totals market
        total_line = row.get("total") or row.get("over_under") or row.get("overUnder")
        over_odds  = row.get("over_odds")  or row.get("overOdds")
        under_odds = row.get("under_odds") or row.get("underOdds")
        if total_line and (over_odds or under_odds):
            tot_outcomes = []
            try:
                pt = float(total_line)
                if over_odds:
                    tot_outcomes.append({"name": "Over",  "price": int(float(over_odds)),  "point": pt})
                if under_odds:
                    tot_outcomes.append({"name": "Under", "price": int(float(under_odds)), "point": pt})
            except (TypeError, ValueError):
                pass
            if tot_outcomes:
                _upsert_market(bk_entry["markets"], "totals", tot_outcomes)

    # Drop events with no bookmakers
    return [e for e in events.values() if e["bookmakers"]]


def _upsert_market(markets: list, key: str, outcomes: list) -> None:
    """Add or merge outcomes into an existing market entry."""
    existing = next((m for m in markets if m["key"] == key), None)
    if existing:
        existing["outcomes"].extend(outcomes)
    else:
        markets.append({"key": key, "outcomes": outcomes})


# ── OddsPapi normalisation ─────────────────────────────────────────────────────

def _parse_oddspapi_response(fixtures: list | dict, sport_key: str) -> list[dict]:
    """
    OddsPapi GET /fixtures/ response → normalised events.
    """
    if not fixtures:
        return []

    rows: list = fixtures if isinstance(fixtures, list) else (
        fixtures.get("data") or fixtures.get("fixtures") or fixtures.get("results") or []
    )

    events: dict[str, dict] = {}

    for row in rows:
        home = row.get("home_team") or row.get("home") or row.get("homeTeam") or ""
        away = row.get("away_team") or row.get("away") or row.get("awayTeam") or ""
        ts   = row.get("commence_time") or row.get("date") or row.get("start_time") or row.get("kickoff") or ""
        ts   = _normalise_ts(str(ts) if ts else None)
        eid  = str(row.get("id") or row.get("fixture_id") or _make_event_id(sport_key, home, away, ts))

        if eid not in events:
            events[eid] = {
                "id":            eid,
                "sport_key":     sport_key,
                "home_team":     home,
                "away_team":     away,
                "commence_time": ts,
                "bookmakers":    [],
            }

        # OddsPapi includes odds nested inside each fixture
        for bk_raw in row.get("bookmakers", []) or row.get("odds", []) or []:
            book_name = bk_raw.get("name") or bk_raw.get("bookmaker") or bk_raw.get("key") or ""
            book = _book_key(book_name)

            bk_entry = next((b for b in events[eid]["bookmakers"] if b["key"] == book), None)
            if not bk_entry:
                bk_entry = {"key": book, "markets": []}
                events[eid]["bookmakers"].append(bk_entry)

            for mkt_raw in bk_raw.get("markets", []) or bk_raw.get("bets", []) or []:
                mkt_name = (mkt_raw.get("key") or mkt_raw.get("name") or "").lower()
                # Normalise market name
                if mkt_name in ("match_winner", "1x2", "h2h", "moneyline", "winner"):
                    mkt_key = "h2h"
                elif mkt_name in ("asian_handicap", "spreads", "handicap", "point_spread"):
                    mkt_key = "spreads"
                elif mkt_name in ("over_under", "totals", "goals_over_under", "total"):
                    mkt_key = "totals"
                else:
                    continue  # skip unknown markets

                outcomes = []
                for sel in mkt_raw.get("values", []) or mkt_raw.get("outcomes", []) or []:
                    name  = sel.get("value") or sel.get("name") or sel.get("selection") or ""
                    price = sel.get("odd")   or sel.get("price") or sel.get("odds") or -110
                    point = sel.get("handicap") or sel.get("point") or sel.get("line")

                    # Map 1X2 labels to team names
                    if name in ("1", "Home"):
                        name = home
                    elif name in ("2", "Away"):
                        name = away
                    elif name in ("X", "Draw"):
                        name = "Draw"

                    try:
                        price_int = int(float(price))
                        entry = {"name": name, "price": price_int}
                        if point is not None:
                            try:
                                entry["point"] = float(point)
                            except (TypeError, ValueError):
                                pass
                        outcomes.append(entry)
                    except (TypeError, ValueError):
                        continue

                if outcomes:
                    _upsert_market(bk_entry["markets"], mkt_key, outcomes)

    return [e for e in events.values() if e["bookmakers"]]


# ── Public API ─────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """Returns True if RAPIDAPI_KEY is set in the environment."""
    return bool(os.getenv("RAPIDAPI_KEY", "").strip())


def fetch_events(sport_key: str) -> list[dict]:
    """
    Fetch events for a sport and return them normalized to the Odds API format.
    Routes to the correct RapidAPI subscription based on sport type.
    Caches results in Redis for 25 minutes.
    """
    cache_key = f"rapidapi:{sport_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("RapidAPI cache hit for %s (%d events)", sport_key, len(cached))
        return cached

    events: list[dict] = []

    if sport_key.startswith("soccer_"):
        # Try OddsPapi first for soccer (wider coverage), fall back to Sportsbook API
        events = _fetch_oddspapi(sport_key)
        if not events and sport_key in _SPORTSBOOK_SPORT_MAP:
            events = _fetch_sportsbook_api(sport_key)
    elif sport_key in _SPORTSBOOK_SPORT_MAP:
        events = _fetch_sportsbook_api(sport_key)
    else:
        logger.debug("RapidAPI: no route for sport_key=%s — skipping", sport_key)
        return []

    if events:
        _cache_set(cache_key, events)
        logger.info("RapidAPI: %d events for %s (cached %ds)", len(events), sport_key, _CACHE_TTL)
    else:
        logger.debug("RapidAPI: 0 events for %s", sport_key)

    return events


def _fetch_sportsbook_api(sport_key: str) -> list[dict]:
    """Fetch from Sportsbook API (US sports)."""
    sport_label = _SPORTSBOOK_SPORT_MAP.get(sport_key)
    if not sport_label:
        return []

    # type=matchups returns upcoming game odds; required by the API
    params = {"type": "matchups", "sport": sport_label}
    data = _get(_SPORTSBOOK_HOST, "/v0/advantages/", params=params)
    if not data:
        logger.warning("Sportsbook API: no data for %s (%s)", sport_key, sport_label)
        return []

    return _parse_sportsbook_response(data, sport_key)


_ODDSPAPI_SPORTS_CACHE_KEY = "rapidapi:oddspapi:sports"
_ODDSPAPI_SPORTS_CACHE_TTL = 6 * 3600  # 6 hours


def _get_oddspapi_sport_id(sport_key: str) -> str | None:
    """
    Resolve sport_key → OddsPapi sport_id.
    Tries live /sports/ discovery first (cached 6h), falls back to static map.
    """
    # 1. Check dynamic cache
    try:
        r = _redis()
        raw = r.get(_ODDSPAPI_SPORTS_CACHE_KEY)
        if raw:
            dynamic_map: dict = json.loads(raw)
            if sport_key in dynamic_map:
                return dynamic_map[sport_key]
    except Exception:
        pass

    # 2. Try to discover from /sports/ endpoint
    try:
        sports_data = _get(_ODDSPAPI_HOST, "/sports/")
        if sports_data:
            rows = sports_data if isinstance(sports_data, list) else (
                sports_data.get("data") or sports_data.get("sports") or sports_data.get("results") or []
            )
            # Build name→id map from API response
            discovered: dict[str, str] = {}
            for s in rows:
                sid   = str(s.get("id") or s.get("sport_id") or "")
                sname = (s.get("name") or s.get("sport") or s.get("league") or "").lower()
                sslug = (s.get("slug") or s.get("key") or "").lower()
                if sid:
                    discovered[sname] = sid
                    if sslug:
                        discovered[sslug] = sid

            # Reverse-match our sport_key to a name in the discovered map
            sport_label = sport_key.replace("soccer_", "").replace("_", " ")
            matched_id = None
            for name, sid in discovered.items():
                if sport_label in name or name in sport_label:
                    matched_id = sid
                    break

            # Cache the raw discovered map for next call
            if discovered:
                try:
                    r = _redis()
                    r.setex(_ODDSPAPI_SPORTS_CACHE_KEY, _ODDSPAPI_SPORTS_CACHE_TTL, json.dumps(discovered))
                except Exception:
                    pass

            if matched_id:
                return matched_id
    except Exception as e:
        logger.debug("OddsPapi /sports/ discovery failed: %s", e)

    # 3. Fall back to static map
    return _ODDSPAPI_SPORT_MAP.get(sport_key)


def _fetch_oddspapi(sport_key: str) -> list[dict]:
    """Fetch from OddsPapi (soccer)."""
    sport_id = _get_oddspapi_sport_id(sport_key)
    if not sport_id:
        logger.debug("OddsPapi: no sport_id mapping for %s", sport_key)
        return []

    params = {"sport_id": sport_id}
    data = _get(_ODDSPAPI_HOST, "/fixtures/", params=params)
    if not data:
        # Some leagues use /odds/ endpoint instead
        data = _get(_ODDSPAPI_HOST, "/odds/", params=params)
    if not data:
        logger.warning("OddsPapi: no data for %s (sport_id=%s)", sport_key, sport_id)
        return []

    return _parse_oddspapi_response(data, sport_key)


def get_active_sports() -> set[str]:
    """
    Return sport keys that currently have events in RapidAPI sources.
    Checks known routable sports — a sport is "active" if it returns ≥1 event.
    Uses cached data where available to avoid redundant API calls.
    """
    if not is_available():
        return set()

    active: set[str] = set()
    all_routable = set(_SPORTSBOOK_SPORT_MAP.keys()) | set(_ODDSPAPI_SPORT_MAP.keys())

    for sport_key in all_routable:
        cache_key = f"rapidapi:{sport_key}"
        cached = _cache_get(cache_key)
        if cached is not None:
            if cached:
                active.add(sport_key)
            continue
        # No cache — do a live probe
        try:
            events = fetch_events(sport_key)
            if events:
                active.add(sport_key)
        except Exception as e:
            logger.debug("RapidAPI active sports probe failed for %s: %s", sport_key, e)

    logger.info("RapidAPI active sports: %d — %s", len(active), sorted(active))
    return active
