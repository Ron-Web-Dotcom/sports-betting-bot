"""
Engine 1 — Odds Collection Engine.

Continuously pulls live odds from The Odds API across all sports and books.
Saves every reading as an OddsSnapshot for line-movement tracking.
Detects new markets and emits alerts.
"""
import logging
import httpx
from datetime import datetime, UTC
from src.core.config import ODDS_API_KEY, ODDS_API_BASE, SPORTS, SPORTSBOOKS
from src.engines.ev_engine import american_to_decimal, implied_prob

logger = logging.getLogger(__name__)

MARKETS = ["h2h", "spreads", "totals"]

# Player prop markets available on 100K+ plan
PLAYER_PROP_MARKETS = [
    # Basketball — player props
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_blocks", "player_steals",
    "player_points_rebounds_assists", "player_points_rebounds",
    "player_points_assists", "player_rebounds_assists",
    "player_turnovers", "player_double_double", "player_triple_double",
    "player_first_basket",
    # Football — player props
    "player_pass_tds", "player_pass_yds", "player_pass_attempts",
    "player_pass_completions", "player_pass_interceptions",
    "player_rush_yds", "player_rush_attempts", "player_rush_tds",
    "player_reception_yds", "player_receptions", "player_receiving_tds",
    "player_longest_reception", "player_longest_rush",
    "player_kicking_points", "player_field_goals",
    "player_sacks", "player_tackles_assists",
    "player_anytime_td",
    # Baseball — player props
    "player_hits", "player_total_bases", "player_strikeouts",
    "player_home_runs", "player_rbis", "player_runs_scored",
    "player_walks", "player_singles", "player_doubles",
    "pitcher_hits_allowed", "pitcher_walks", "pitcher_earned_runs",
    "pitcher_outs", "pitcher_record_a_win",
    "batter_hits", "batter_total_bases", "batter_home_runs",
    "batter_rbis", "batter_runs_scored", "batter_strikeouts",
    # Hockey — player props
    "player_shots_on_goal", "player_points", "player_goals",
    "player_assists", "player_saves", "player_blocked_shots",
    "player_power_play_points",
    # Soccer — player props
    "player_shots_on_target", "player_goals", "player_assists",
    "player_cards", "player_shots", "player_tackles",
    "player_to_score_anytime", "player_to_score_first",
    # Golf — player props
    "player_top_5_finish", "player_top_10_finish", "player_top_20_finish",
    "player_make_cut", "player_win",
    # MMA / Boxing — player props
    "player_method_of_victory", "player_total_rounds",
    "player_win_by_ko", "player_win_by_submission", "player_win_by_decision",
    # Tennis — player props
    "player_sets_won", "player_games_won", "player_to_win_set",
]

# Soccer — all leagues share same prop market names
_SOCCER_LEAGUES = {
    # US
    "soccer_usa_mls", "soccer_usa_nwsl",
    # Europe Top 5
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one",
    # Europe Other
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_turkey_super_league", "soccer_spl",
    "soccer_belgium_first_div", "soccer_greece_super_league",
    "soccer_denmark_superliga", "soccer_sweden_allsvenskan",
    "soccer_norway_eliteserien", "soccer_finland_veikkausliiga",
    "soccer_austria_bundesliga", "soccer_swiss_superleague",
    "soccer_czech_liga", "soccer_poland_ekstraklasa",
    "soccer_romania_liga_1", "soccer_croatia_hnl",
    # UEFA / Cups
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league", "soccer_uefa_nations_league",
    "soccer_fifa_world_cup", "soccer_fifa_club_world_cup",
    "soccer_africa_cup_of_nations",
    # South America
    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
    "soccer_mexico_ligamx", "soccer_conmebol_copa_america",
    "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
    "soccer_chile_primera_division", "soccer_colombia_primera_a",
    "soccer_ecuador_liga_pro", "soccer_uruguay_primera_division",
    "soccer_peru_primera_division", "soccer_venezuela_primera_liga",
    # Asia / Oceania / Middle East
    "soccer_japan_j_league", "soccer_south_korea_kleague1",
    "soccer_china_superleague", "soccer_saudi_arabia_premier_league",
    "soccer_australia_aleague",
    # Women's soccer
    "soccer_fifa_womens_world_cup", "soccer_uefa_womens_champs_league",
    "soccer_england_wsl", "soccer_germany_frauen_bundesliga",
    "soccer_spain_liga_f", "soccer_france_d1_feminine",
    "soccer_italy_serie_a_feminine",
}

# Golf tournaments
_GOLF_TOURNAMENTS = {
    "golf_pga_tour",
    "golf_masters_tournament_winner", "golf_pga_championship_winner",
    "golf_us_open_winner", "golf_the_open_championship_winner",
    "golf_lpga",
    "golf_dp_world_tour",
}

# Tennis — grand slams + all active tour events
_TENNIS = {
    # Grand Slams (ATP + WTA)
    "tennis_atp_french_open",       "tennis_wta_french_open",
    "tennis_atp_wimbledon",         "tennis_wta_wimbledon",
    "tennis_atp_us_open",           "tennis_wta_us_open",
    "tennis_atp_australian_open",   "tennis_wta_aus_open_singles",
    # ATP Masters 1000 + Tour events
    "tennis_atp_madrid",            "tennis_atp_rome",
    "tennis_atp_miami",             "tennis_atp_indian_wells",
    "tennis_atp_toronto",           "tennis_atp_cincinnati",
    "tennis_atp_queens_club_champ", "tennis_atp_halle_open",
    # WTA tour events
    "tennis_wta_german_open",
}

# All sports that support player props on Odds API
PLAYER_PROP_SPORTS = {
    "basketball_nba", "basketball_wnba", "basketball_ncaab", "basketball_wncaab",
    "americanfootball_nfl", "americanfootball_ncaaf",
    "baseball_mlb",
    "icehockey_nhl",
    "mma_mixed_martial_arts", "boxing_boxing",
    "aussierules_afl", "aussierules_aflw",
    "icehockey_pwhl",
    # Cricket — men + women
    "cricket_icc_world_cup", "cricket_ipl", "cricket_icc_womens_t20_wc",
    "cricket_test_match", "cricket_odi", "cricket_international_t20",
    "cricket_t20_world_cup_womens", "cricket_t20_blast",
    # Rugby union + league
    "rugbyunion_world_cup", "rugbyunion_women_world_cup",
    "rugbyunion_super_rugby", "rugbyunion_premiership",
    "rugbyunion_top14", "rugbyunion_united_rugby_championship",
    "rugbyleague_nrl", "rugbyleague_nrl_state_of_origin",
    # Motorsport
    "motorsport_formula_1", "motorsport_indycar", "motorsport_nascar_cup_series",
} | _SOCCER_LEAGUES | _GOLF_TOURNAMENTS | _TENNIS


# ── Redis helpers for credits-exhausted flag ───────────────────────────────────

_CREDITS_FLAG = "oddsapi:credits_exhausted"
_CREDITS_FLAG_TTL = 3600  # 1 hour


def _redis_client():
    from src.core.config import REDIS_URL
    import redis
    return redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


def _credits_exhausted() -> bool:
    """Return True if the credits-exhausted flag is set in Redis."""
    try:
        return bool(_redis_client().get(_CREDITS_FLAG))
    except Exception:
        return False


def _set_credits_exhausted() -> None:
    """Set the credits-exhausted flag with a 1h TTL."""
    try:
        _redis_client().setex(_CREDITS_FLAG, _CREDITS_FLAG_TTL, "1")
        logger.warning("Odds API credits exhausted — flag set for %ds, scans paused until credits reset", _CREDITS_FLAG_TTL)
    except Exception as e:
        logger.warning("Could not set credits_exhausted flag: %s", e)


def _clear_credits_exhausted() -> None:
    """Clear the credits-exhausted flag — only logs if the flag was actually set."""
    try:
        r = _redis_client()
        if r.exists(_CREDITS_FLAG):
            r.delete(_CREDITS_FLAG)
            logger.info("Odds API credits restored — cleared credits_exhausted flag")
    except Exception:
        pass


# ── Raw API calls ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict | list | None:
    params["apiKey"] = ODDS_API_KEY
    try:
        r = httpx.get(f"{ODDS_API_BASE}{path}", params=params, timeout=20)
        if r.status_code == 429:
            _set_credits_exhausted()
            return None
        if r.status_code in (401, 403):
            logger.warning("OddsAPI %s → %s (auth/forbidden — not marking credits exhausted)", path, r.status_code)
            return None
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining", "?")
        logger.info("OddsAPI %s → OK (credits remaining: %s)", path, remaining)
        _clear_credits_exhausted()
        return r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            _set_credits_exhausted()
        elif e.response.status_code in (404, 422):
            logger.warning("OddsAPI %s → %s (off-season/unavailable)", path, e.response.status_code)
        else:
            logger.error("OddsAPI error %s: %s", path, e)
        return None
    except httpx.HTTPError as e:
        logger.error("OddsAPI error %s: %s", path, e)
        return None


def fetch_events(sport_key: str) -> list[dict]:
    if _credits_exhausted():
        return []
    result = _get(f"/sports/{sport_key}/odds", {
        "regions":    "us",
        "markets":    ",".join(MARKETS),
        "oddsFormat": "american",
        "bookmakers": ",".join(SPORTSBOOKS),
    })
    return result if result is not None else []


def fetch_scores(sport_key: str, days_from: int = 1) -> list[dict]:
    result = _get(f"/sports/{sport_key}/scores", {"daysFrom": days_from})
    return result or []


def fetch_event_odds(sport_key: str, event_id: str) -> dict | None:
    return _get(f"/sports/{sport_key}/events/{event_id}/odds", {
        "regions": "us", "markets": ",".join(MARKETS), "oddsFormat": "american",
    })


def fetch_player_props(sport_key: str, event_id: str) -> list[dict]:
    """
    Fetch player props for a single event from Odds API.
    Returns normalised list of props: {player, stat, line, over_odds, under_odds, books_odds}.
    Costs 1 credit per market per event — fetch only active events.
    """
    if sport_key not in PLAYER_PROP_SPORTS:
        return []
    if _credits_exhausted():
        return []

    _SOCCER_MARKETS     = ["player_shots_on_target", "player_goals", "player_assists", "player_cards"]
    _BASKETBALL_MARKETS = ["player_points", "player_rebounds", "player_assists", "player_threes"]
    _TENNIS_MARKETS     = ["player_games_won", "player_sets_won"]

    # Game prop markets — over/under and result-based game props
    _GAME_SOCCER     = ["btts", "draw_no_bet", "double_chance", "h2h_corners", "h2h_cards"]
    _GAME_BASKETBALL = ["team_points_q1", "team_points_q2", "alternate_totals"]
    _GAME_BASEBALL   = ["innings_1_5_total", "alternate_totals"]
    _GAME_HOCKEY     = ["alternate_totals"]
    _GAME_FOOTBALL   = ["team_points_q1", "alternate_totals", "alternate_spreads"]

    # Team prop markets — available on HardRock and most books
    _TEAM_BASKETBALL = [
        "team_totals", "team_first_basket", "team_points_q1", "team_points_q2",
        "team_points_q3", "team_points_q4", "team_blocks", "team_rebounds",
        "team_assists", "team_threes",
    ]
    _TEAM_FOOTBALL = [
        "team_totals", "team_first_td_scorer", "team_points_q1",
        "team_rushing_yards", "team_passing_yards", "team_first_to_score",
    ]
    _TEAM_BASEBALL = [
        "team_totals", "team_first_to_score", "team_hits",
        "team_runs_q1", "team_strikeouts",
    ]
    _TEAM_HOCKEY = [
        "team_totals", "team_first_goal_scorer", "team_shots_on_goal",
        "team_power_play_goals",
    ]
    _TEAM_SOCCER = [
        "team_totals", "team_first_goal", "team_corners",
        "team_shots_on_target", "team_to_score_in_both_halves",
    ]

    _BASKETBALL_FULL = [
        "player_points", "player_rebounds", "player_assists", "player_threes",
        "player_blocks", "player_steals", "player_points_rebounds_assists",
        "player_points_rebounds", "player_points_assists", "player_rebounds_assists",
        "player_turnovers", "player_double_double", "player_first_basket",
    ]
    _FOOTBALL_FULL = [
        "player_pass_tds", "player_pass_yds", "player_pass_attempts",
        "player_pass_completions", "player_rush_yds", "player_rush_attempts",
        "player_rush_tds", "player_reception_yds", "player_receptions",
        "player_receiving_tds", "player_anytime_td", "player_sacks",
        "player_tackles_assists",
    ]
    _BASEBALL_FULL = [
        "batter_home_runs", "batter_hits", "batter_total_bases", "batter_rbis",
        "batter_runs_scored", "batter_strikeouts", "pitcher_strikeouts",
        "pitcher_hits_allowed", "pitcher_walks", "pitcher_earned_runs",
        "pitcher_outs", "pitcher_record_a_win",
    ]
    _HOCKEY_FULL = [
        "player_shots_on_goal", "player_points", "player_goals",
        "player_assists", "player_blocked_shots", "player_power_play_points",
    ]
    _MMA_FULL = [
        "player_method_of_victory", "player_total_rounds",
        "player_win_by_ko", "player_win_by_submission", "player_win_by_decision",
    ]

    _sport_markets = {
        "basketball_nba":               _BASKETBALL_FULL + _TEAM_BASKETBALL + _GAME_BASKETBALL,
        "basketball_wnba":              _BASKETBALL_FULL + _TEAM_BASKETBALL + _GAME_BASKETBALL,
        "basketball_ncaab":             ["player_points", "player_rebounds", "player_assists", "player_threes", "player_double_double"] + _TEAM_BASKETBALL + _GAME_BASKETBALL,
        "basketball_wncaab":            ["player_points", "player_rebounds", "player_assists", "player_threes"] + _TEAM_BASKETBALL + _GAME_BASKETBALL,
        "americanfootball_nfl":         _FOOTBALL_FULL + _TEAM_FOOTBALL + _GAME_FOOTBALL,
        "americanfootball_ncaaf":       ["player_pass_tds", "player_pass_yds", "player_rush_yds", "player_reception_yds", "player_anytime_td"] + _TEAM_FOOTBALL + _GAME_FOOTBALL,
        "baseball_mlb":                 _BASEBALL_FULL + _TEAM_BASEBALL + _GAME_BASEBALL,
        "icehockey_nhl":                _HOCKEY_FULL + _TEAM_HOCKEY + _GAME_HOCKEY,
        "mma_mixed_martial_arts":       _MMA_FULL,
        "boxing_boxing":                _MMA_FULL,
        # Aussie Rules
        "aussierules_afl":              ["player_goals", "player_disposals", "player_marks", "player_tackles"],
        "aussierules_aflw":             ["player_goals", "player_disposals", "player_marks", "player_tackles"],
        # Ice hockey — PWHL (women)
        "icehockey_pwhl":               _HOCKEY_FULL,
        # Cricket — all formats
        **{sk: ["player_runs", "player_wickets", "player_fours", "player_sixes",
                "player_fifties", "player_hundreds", "player_catches"]
           for sk in [
               "cricket_icc_world_cup", "cricket_ipl", "cricket_icc_womens_t20_wc",
               "cricket_test_match", "cricket_odi", "cricket_international_t20",
               "cricket_t20_world_cup_womens", "cricket_t20_blast",
           ]},
        # Rugby union + league
        **{sk: ["player_tries", "player_conversions", "player_penalty_goals",
                "player_tackles", "player_carries"]
           for sk in [
               "rugbyunion_world_cup", "rugbyunion_women_world_cup",
               "rugbyunion_super_rugby", "rugbyunion_premiership",
               "rugbyunion_top14", "rugbyunion_united_rugby_championship",
               "rugbyleague_nrl", "rugbyleague_nrl_state_of_origin",
           ]},
        # Motorsport — race/podium/fastest-lap outrights only
        **{sk: ["player_podium_finish", "player_race_winner", "player_fastest_lap",
                "player_top_5_finish", "player_top_10_finish"]
           for sk in [
               "motorsport_formula_1", "motorsport_indycar", "motorsport_nascar_cup_series",
           ]},
        # Golf — outrights
        **{sk: ["player_win", "player_top_5_finish", "player_top_10_finish",
                "player_top_20_finish", "player_make_cut"]
           for sk in _GOLF_TOURNAMENTS},
        # All tennis events (ATP + WTA — slams and tour)
        **{t: _TENNIS_MARKETS for t in _TENNIS},
        # All soccer leagues (men + women, all regions)
        **{league: _SOCCER_MARKETS + _TEAM_SOCCER + _GAME_SOCCER for league in _SOCCER_LEAGUES},
    }
    markets = _sport_markets.get(sport_key, [])
    if not markets:
        return []

    data = _get(f"/sports/{sport_key}/events/{event_id}/odds", {
        "regions":    "us",
        "markets":    ",".join(markets),
        "oddsFormat": "american",
        "bookmakers": ",".join(SPORTSBOOKS),
    })
    if not data:
        return []

    _TEAM_MARKET_PREFIXES = ("team_",)

    props = []
    for bk in data.get("bookmakers", []):
        book = bk.get("key")
        if not book:
            continue
        for mkt in bk.get("markets", []):
            mkt_key    = mkt.get("key")
            if not mkt_key:
                continue
            is_team    = mkt_key.startswith(_TEAM_MARKET_PREFIXES)
            stat       = mkt_key.replace("player_", "").replace("team_", "").replace("_", " ").title()
            for outcome in mkt.get("outcomes", []):
                player     = outcome.get("description", outcome.get("name", ""))
                direction  = outcome.get("name", "").lower()  # "Over" or "Under"
                line       = outcome.get("point")
                try:
                    odds = int(round(float(outcome.get("price", -110))))
                except (TypeError, ValueError):
                    odds = -110

                # Find or create prop entry keyed by (player, stat, line)
                key = (player, stat, line)
                existing = next((p for p in props if (p["player"], p["stat"], p["line"]) == key), None)
                if not existing:
                    existing = {
                        "player":       player,
                        "stat":         stat,
                        "line":         line,
                        "over_odds":    {},
                        "under_odds":   {},
                        "sport_key":    sport_key,
                        "event_id":     event_id,
                        "source":       "odds_api",
                        "is_team_prop": is_team,
                    }
                    props.append(existing)

                if direction == "over":
                    existing["over_odds"][book] = odds
                elif direction == "under":
                    existing["under_odds"][book] = odds

    logger.info("OddsAPI player props: %d props for event %s (%s)", len(props), event_id, sport_key)
    return props


def _get_active_sports_cached() -> set[str]:
    """
    Get today's active sports from Redis cache.
    If cache is empty, call Sofascore and cache result until midnight ET.
    Returns ALL sport_keys that are active — including player prop variants.
    """
    import json
    from src.core.config import REDIS_URL
    try:
        import redis as _redis
        from src.core.timezone import et_naive
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        cached = r.get("sofascore:active_sports")
        if cached:
            try:
                return set(json.loads(cached))
            except (json.JSONDecodeError, TypeError):
                pass

        # Cache miss — fetch from Sofascore
        from src.apis.sofascore import get_active_sports_today
        active_from_sofascore = get_active_sports_today()

        from src.apis.sofascore import SPORT_MAP as _SM
        slug_to_keys: dict[str, list[str]] = {}
        for sk, slug in _SM.items():
            slug_to_keys.setdefault(slug, []).append(sk)

        active: set[str] = set(active_from_sofascore)
        for sk in active_from_sofascore:
            slug = _SM.get(sk)
            if slug:
                active.update(slug_to_keys.get(slug, []))

        for sk in list(PLAYER_PROP_SPORTS):
            slug = _SM.get(sk)
            if slug and any(_SM.get(a) == slug for a in active_from_sofascore):
                active.add(sk)

        # Never cache an empty set — if Sofascore returned nothing, fall back to all known sports
        if not active:
            logger.warning("Active sports: Sofascore returned empty — defaulting to all sports")
            return set(PLAYER_PROP_SPORTS)

        now_et = et_naive()
        from datetime import datetime as _dt, timedelta
        midnight = _dt.combine(now_et.date(), _dt.min.time()) + timedelta(days=1)
        ttl = max(int((midnight - now_et).total_seconds()), 3600)
        r.setex("sofascore:active_sports", ttl, json.dumps(list(active)))
        logger.info("Active sports cached (%d): %s", len(active), sorted(active))
        return active
    except Exception as e:
        logger.warning("Active sports cache failed: %s — defaulting to all sports", e)
        return set(PLAYER_PROP_SPORTS)


def fetch_all_player_props(all_events: dict[str, list[dict]]) -> list[dict]:
    """
    Fetch player props only for sports Sofascore confirms have games today.
    Active sports are cached in Redis until midnight — Sofascore is only
    called once per day, not on every props scan.
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from dateutil.parser import parse as _parse
    from zoneinfo import ZoneInfo

    _ET      = ZoneInfo("America/New_York")
    cutoff   = datetime.now(_ET) + timedelta(hours=24)   # ET throughout
    tasks    = []
    skipped_no_props = 0
    for sport_key, events in all_events.items():
        if sport_key not in PLAYER_PROP_SPORTS:
            skipped_no_props += 1
            continue
        # If scan_all_sports returned events for this sport, it's active — no need
        # to double-gate against Sofascore cache (which can be stale or miss sports)
        if not events:
            continue
        for ev in events:
            ct = ev.get("commence_time")
            try:
                if isinstance(ct, str):
                    ct = _parse(ct)
                if ct:
                    # Convert API UTC time → ET on arrival, compare in ET
                    ct_aware = ct if ct.tzinfo else ct.replace(tzinfo=UTC)
                    ct_et    = ct_aware.astimezone(_ET)
                    if ct_et > cutoff:
                        continue
            except Exception:
                pass
            if ev.get("id"):
                tasks.append((sport_key, ev["id"]))

    logger.info(
        "fetch_all_player_props: %d tasks queued from %d sports (%d sports skipped — no prop markets)",
        len(tasks), len(all_events) - skipped_no_props, skipped_no_props,
    )
    if not tasks:
        logger.warning(
            "fetch_all_player_props: 0 tasks — all_events had %d sports, "
            "none matched PLAYER_PROP_SPORTS or all events were future-filtered",
            len(all_events),
        )
        return []

    all_props = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_player_props, sk, eid): (sk, eid) for sk, eid in tasks}
        for fut, (sk, eid) in futures.items():
            try:
                all_props.extend(fut.result())
            except Exception as e:
                logger.warning("Player props fetch failed [%s/%s]: %s", sk, eid, e)

    logger.info("OddsAPI player props total: %d across %d events", len(all_props), len(tasks))
    return all_props


# ── Normalised data structures ─────────────────────────────────────────────────

def normalise_event(event: dict, sport_key: str) -> dict:
    """Flatten a raw Odds API event into a consistent structure."""
    markets: dict = {}
    for bk in event.get("bookmakers", []):
        book = bk.get("key")
        if not book:
            continue
        for mkt in bk.get("markets", []):
            mk = mkt.get("key")
            if not mk:
                continue
            markets.setdefault(mk, {})
            for outcome in mkt.get("outcomes", []):
                sel   = outcome.get("name", "")
                if not sel:
                    continue
                try:
                    price = int(round(float(outcome.get("price", -110))))
                except (TypeError, ValueError):
                    price = -110
                line  = outcome.get("point")
                markets[mk].setdefault(sel, [])
                markets[mk][sel].append({
                    "book":          book,
                    "american_odds": price,
                    "decimal_odds":  american_to_decimal(price),
                    "implied_prob":  implied_prob(price),
                    "line":          line,
                })

    # Sort each selection by decimal_odds descending (best odds first)
    for mk in markets:
        for sel in markets[mk]:
            markets[mk][sel].sort(key=lambda x: x["decimal_odds"], reverse=True)

    return {
        "id":           event.get("id"),
        "sport":        sport_key,
        "home_team":    event.get("home_team", ""),
        "away_team":    event.get("away_team", ""),
        "name":         f"{event.get('away_team','')} vs {event.get('home_team','')}",
        "commence_time":event.get("commence_time"),
        "markets":      markets,
    }


def best_odds(event_norm: dict, market: str, selection: str) -> dict | None:
    """Return the best (highest) odds entry for a selection across all books."""
    entries = event_norm["markets"].get(market, {}).get(selection, [])
    return entries[0] if entries else None


# ── Full scan ──────────────────────────────────────────────────────────────────

def get_live_active_sport_keys() -> set[str]:
    """
    Ask the Odds API which sports currently have active events.
    Costs 1 credit. Cached in Redis for 6 hours.
    Returns the set of sport keys that are actually in season RIGHT NOW —
    no hardcoded dates, works correctly across year boundaries.
    """
    import json
    from src.core.config import REDIS_URL, ODDS_API_KEY, ODDS_API_BASE
    _CACHE_KEY = "oddsapi:active_sport_keys"
    _TTL = 6 * 3600  # 6 hours

    try:
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        cached = r.get(_CACHE_KEY)
        if cached:
            try:
                return set(json.loads(cached))
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception:
        r = None

    # Live call — /sports returns only sports with upcoming events
    try:
        from src.apis.base import get_json
        data = get_json(f"{ODDS_API_BASE}/sports", {"apiKey": ODDS_API_KEY, "all": "false"})
        if not data or not isinstance(data, list):
            logger.warning("Odds API /sports returned no data — falling back to full SPORTS list")
            return set(SPORTS.values())

        # has_outrights=True means it's an outrights-only market (e.g. season winner)
        # Golf and tennis have real game events even when has_outrights=True, so include them
        _golf_tennis = {"golf", "tennis"}
        active = {
            s["key"] for s in data
            if s.get("key") and (
                not s.get("has_outrights", False) or
                any(gt in s["key"].lower() for gt in _golf_tennis)
            )
        }
        logger.info("Odds API: %d active in-season sports", len(active))

        if r and active:
            try:
                r.setex(_CACHE_KEY, _TTL, json.dumps(list(active)))
            except Exception:
                pass
        return active
    except Exception as e:
        logger.warning("get_live_active_sport_keys failed: %s — using full list", e)
        return set(SPORTS.values())


def scan_all_sports() -> dict[str, list[dict]]:
    """
    Scan Odds API for prices on today's Sofascore-confirmed games only.
    Sofascore is the source of truth for what's playing today and when.
    Odds API only provides prices (moneylines, spreads, totals) for those games.
    """
    import redis as _redis
    import json as _json
    from src.core.config import REDIS_URL

    # Load Sofascore confirmed today games — source of truth
    sf_teams: set[str] = set()
    sf_sport_keys: set[str] = set()
    try:
        _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        for _sfkey in ("sofascore:day_games", "sofascore:night_games"):
            _raw = _r.get(_sfkey)
            if _raw:
                for ev in _json.loads(_raw):
                    if ev.get("home_team"):
                        sf_teams.add(ev["home_team"].lower())
                    if ev.get("away_team"):
                        sf_teams.add(ev["away_team"].lower())
                    if ev.get("sport"):
                        sf_sport_keys.add(ev["sport"])
    except Exception as _e:
        logger.warning("scan_all_sports: Sofascore cache read failed: %s", _e)

    # Derive Odds API sport keys from Sofascore's active sports
    known_keys  = set(SPORTS.values())
    active_keys = get_live_active_sport_keys()
    if sf_sport_keys:
        # Map Sofascore sport slugs → Odds API keys via SPORTS alias map
        from src.apis.sofascore import SPORT_MAP as _SM
        _sf_odds_keys: set[str] = set()
        for sf_slug in sf_sport_keys:
            for odds_key, sf_mapped in _SM.items() if hasattr(_SM, "items") else []:
                if sf_mapped == sf_slug and odds_key in known_keys:
                    _sf_odds_keys.add(odds_key)
        # Also keep any active key that directly matches a Sofascore sport slug
        for k in known_keys:
            if k in sf_sport_keys or any(k.startswith(s.split("_")[0]) for s in sf_sport_keys):
                _sf_odds_keys.add(k)
        sport_keys = (_sf_odds_keys & active_keys) or (known_keys & active_keys)
    else:
        sport_keys = known_keys & active_keys

    logger.info("OddsAPI scanning %d sport keys (Sofascore confirmed %d teams across %d sports)",
                len(sport_keys), len(sf_teams), len(sf_sport_keys))

    result: dict[str, list[dict]] = {}
    raw_events: dict[str, list[dict]] = {}  # unfiltered, kept as fallback

    for sport_key in sport_keys:
        events = fetch_events(sport_key)
        if not events:
            continue
        raw_events[sport_key] = events

        # Filter to only games where at least one team is in Sofascore's today list
        if sf_teams:
            def _team_match(team: str) -> bool:
                tl = team.lower()
                return tl in sf_teams or any(
                    tl in sf or sf in tl or
                    any(tok in sf or sf in tok for tok in tl.split() if len(tok) > 3)
                    for sf in sf_teams
                )
            filtered = [
                e for e in events
                if _team_match(e.get("home_team", "")) or _team_match(e.get("away_team", ""))
            ]
        else:
            filtered = events

        if filtered:
            result[sport_key] = [normalise_event(e, sport_key) for e in filtered]
            logger.info("Odds: %d events for %s (Sofascore-filtered)", len(filtered), sport_key)

    if not result and raw_events:
        # Sofascore filter wiped everything — fall back to all active-sport events
        # so props still get fetched. Sofascore team cross-check in entry builders
        # acts as the second quality gate.
        logger.warning(
            "OddsAPI: 0 events after Sofascore filter (sf_teams=%d) — "
            "falling back to unfiltered events (%d sports)",
            len(sf_teams), len(raw_events),
        )
        for sport_key, events in raw_events.items():
            result[sport_key] = [normalise_event(e, sport_key) for e in events]
    elif not result:
        logger.warning("OddsAPI: 0 events after Sofascore filter — check Sofascore cache")
    return result


def save_snapshots_to_db(all_events: dict[str, list[dict]]) -> None:
    """Persist all current odds as OddsSnapshots. Upserts Game records on the fly."""
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from dateutil.parser import parse as _parse
    from zoneinfo import ZoneInfo as _ZoneInfo

    with get_db() as db:
        for sport_key, events in all_events.items():
            for event in events:
                ext_id = event["id"]

                # Upsert game — create if missing, update commence_time if changed
                game = db.query(Game).filter_by(external_id=ext_id).first()
                if not game:
                    ct_raw = event.get("commence_time")
                    try:
                        ct = _parse(ct_raw).replace(tzinfo=None) if isinstance(ct_raw, str) else ct_raw
                    except Exception:
                        ct = datetime.now(_ZoneInfo("America/New_York")).astimezone(UTC).replace(tzinfo=None)
                    game = Game(
                        external_id   = ext_id,
                        sport         = sport_key,
                        home_team     = event.get("home_team", ""),
                        away_team     = event.get("away_team", ""),
                        commence_time = ct,
                    )
                    db.add(game)
                    db.flush()  # get game.id immediately

                for market, selections in event["markets"].items():
                    for sel, entries in selections.items():
                        for entry in entries:
                            snap = OddsSnapshot(
                                game_id      = game.id,
                                book         = entry["book"],
                                market       = market,
                                selection    = sel,
                                american_odds= entry["american_odds"],
                                decimal_odds = entry["decimal_odds"],
                                implied_prob = entry["implied_prob"],
                                line_value   = entry.get("line"),
                            )
                            db.add(snap)


def run_full_odds_scan() -> list[dict]:
    """Scan all sports, save to DB, detect line movements, return flat list of snapshots."""
    all_events = scan_all_sports()
    save_snapshots_to_db(all_events)

    # Detect and persist line movements after each scan
    try:
        from src.engines.line_movement_engine import detect_movements, save_movement
        current  = get_latest_snapshots_by_game()
        opening  = get_opening_line_by_game()
        for game_id, cur_snaps in current.items():
            open_snaps = opening.get(game_id, [])
            if not open_snaps:
                continue
            # Build combined list: opening snaps first (oldest), then current
            combined = open_snaps + cur_snaps
            game_label = f"{cur_snaps[0].get('away_team','')} @ {cur_snaps[0].get('home_team','')}"
            alerts = detect_movements(game_id, game_label, combined, window_minutes=180)
            for alert in alerts:
                try:
                    save_movement(alert)
                except Exception:
                    pass
        logger.info("Line movement detection: %d games scanned", len(current))
    except Exception as _lme:
        logger.warning("Line movement detection failed: %s", _lme)

    flat = []
    for sport_key, events in all_events.items():
        for ev in events:
            flat.append({"sport_key": sport_key, **ev})
    return flat


def get_latest_snapshots_by_game() -> dict[int, list[dict]]:
    """Return {game_id: [snapshot_dicts]} for games commencing within the next 24 hours.

    The 8 AM Sofascore scan runs daily and covers the next 24 h. At 8 AM
    the next day a fresh scan replaces it. So a rolling 24 h window here
    always aligns with exactly one day's slate.
    Sofascore cross-reference in the entry builders is the quality gate for
    confirming which games are actually on today's card.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
    # All time math anchored to ET.
    # commence_time is stored as naive UTC in the DB, so we convert ET→UTC for filters.
    now_et_aware = datetime.now(_ET)
    now_et       = now_et_aware.replace(tzinfo=None)          # naive ET (matches captured_at column)
    now_utc      = now_et_aware.astimezone(UTC).replace(tzinfo=None)   # naive UTC (matches commence_time column)

    # Snapshot freshness: captured within last 6 ET hours
    cutoff_lo = now_et - timedelta(hours=6)

    # Game window: include games that started up to 3 hours ago (props still live)
    # through end of tomorrow so the full day+night slate is always covered.
    cutoff_started = now_utc - timedelta(hours=3)
    et_eod = now_et_aware.replace(hour=23, minute=59, second=59) + timedelta(days=1)
    cutoff_hi = et_eod.astimezone(UTC).replace(tzinfo=None)

    result: dict[int, list[dict]] = {}
    with get_db() as db:
        rows = (
            db.query(OddsSnapshot, Game)
            .join(Game, Game.id == OddsSnapshot.game_id)
            .filter(
                OddsSnapshot.captured_at >= cutoff_lo,
                Game.commence_time != None,
                Game.commence_time >= cutoff_started,  # include games started <3h ago
                Game.commence_time <  cutoff_hi,       # within ET end-of-tomorrow
            )
            .limit(5000)  # guard: 8 books × ~5 markets × ~100 games = ~4000 max
            .all()
        )
        for snap, game in rows:
            ct = game.commence_time
            ct_str = (ct.strftime("%Y-%m-%dT%H:%M:%SZ") if ct else "")
            result.setdefault(snap.game_id, []).append({
                "book":          snap.book,
                "market":        snap.market,
                "selection":     snap.selection,
                "best_odds":     snap.american_odds,
                "american_odds": snap.american_odds,
                "decimal_odds":  snap.decimal_odds,
                "line_value":    snap.line_value,
                "captured_at":   snap.captured_at,   # needed by detect_movements
                "sport_key":     game.sport,
                "home_team":     game.home_team,
                "away_team":     game.away_team,
                "commence_time": ct_str,
            })
    return result


def get_opening_line_by_game() -> dict[int, list[dict]]:
    """Return the FIRST (opening) snapshot per game/market/selection across all books.
    Used to compare opening line vs current line for steam/sharp detection.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import func

    _ET = ZoneInfo("America/New_York")
    now_utc = datetime.now(_ET).astimezone(UTC).replace(tzinfo=None)
    cutoff_started = now_utc - timedelta(hours=24)
    cutoff_hi      = now_utc + timedelta(days=2)

    result: dict[int, list[dict]] = {}
    with get_db() as db:
        # Subquery: earliest captured_at per (game_id, market, selection, book)
        subq = (
            db.query(
                OddsSnapshot.game_id,
                OddsSnapshot.market,
                OddsSnapshot.selection,
                OddsSnapshot.book,
                func.min(OddsSnapshot.captured_at).label("first_at"),
            )
            .join(Game, Game.id == OddsSnapshot.game_id)
            .filter(
                Game.commence_time >= cutoff_started,
                Game.commence_time <  cutoff_hi,
            )
            .group_by(
                OddsSnapshot.game_id,
                OddsSnapshot.market,
                OddsSnapshot.selection,
                OddsSnapshot.book,
            )
            .subquery()
        )
        rows = (
            db.query(OddsSnapshot, Game)
            .join(Game, Game.id == OddsSnapshot.game_id)
            .join(
                subq,
                (OddsSnapshot.game_id   == subq.c.game_id)  &
                (OddsSnapshot.market    == subq.c.market)    &
                (OddsSnapshot.selection == subq.c.selection) &
                (OddsSnapshot.book      == subq.c.book)      &
                (OddsSnapshot.captured_at == subq.c.first_at),
            )
            .limit(5000)  # opening-line set is same size as current — cap matches
            .all()
        )
        for snap, game in rows:
            ct = game.commence_time
            ct_str = ct.strftime("%Y-%m-%dT%H:%M:%SZ") if ct else ""
            result.setdefault(snap.game_id, []).append({
                "book":          snap.book,
                "market":        snap.market,
                "selection":     snap.selection,
                "american_odds": snap.american_odds,
                "decimal_odds":  snap.decimal_odds,
                "line_value":    snap.line_value,
                "captured_at":   snap.captured_at,
                "sport_key":     game.sport,
                "home_team":     game.home_team,
                "away_team":     game.away_team,
                "commence_time": ct_str,
            })
    return result
