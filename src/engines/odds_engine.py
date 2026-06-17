"""
Engine 1 — Odds Collection Engine.

Continuously pulls live odds from The Odds API across all sports and books.
Saves every reading as an OddsSnapshot for line-movement tracking.
Detects new markets and emits alerts.
"""
import logging
import httpx
from datetime import datetime
from src.core.config import ODDS_API_KEY, ODDS_API_BASE, SPORTS, SPORTSBOOKS
from src.core.timezone import et_naive
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
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_usa_mls",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_mexico_ligamx", "soccer_argentina_primera_division",
    "soccer_brazil_campeonato", "soccer_turkey_super_league", "soccer_spl",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_fifa_world_cup", "soccer_fifa_club_world_cup",
    "soccer_conmebol_copa_america",
    "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
    "soccer_africa_cup_of_nations",
    # Women's soccer
    "soccer_usa_nwsl", "soccer_fifa_womens_world_cup",
    "soccer_uefa_womens_champs_league",
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
}

# Tennis — grand slams + all active tour events
_TENNIS = {
    "tennis_atp_french_open",       "tennis_wta_french_open",
    "tennis_atp_wimbledon",         "tennis_wta_wimbledon",
    "tennis_atp_us_open",           "tennis_wta_us_open",
    "tennis_atp_australian_open",   "tennis_wta_aus_open_singles",
    "tennis_atp_queens_club_champ", "tennis_atp_halle_open",
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
    "cricket_icc_world_cup", "cricket_ipl", "cricket_icc_womens_t20_wc",
    "cricket_test_match", "cricket_odi", "cricket_international_t20",
    "cricket_t20_world_cup_womens",
    "rugbyunion_world_cup", "rugbyunion_women_world_cup",
    "rugbyleague_nrl", "rugbyleague_nrl_state_of_origin",
} | _SOCCER_LEAGUES | _GOLF_TOURNAMENTS | _TENNIS


# ── Raw API calls ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict | list | None:
    params["apiKey"] = ODDS_API_KEY
    try:
        r = httpx.get(f"{ODDS_API_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining", "?")
        logger.info("OddsAPI %s → OK (credits remaining: %s)", path, remaining)
        return r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            logger.warning("OddsAPI %s → %s (off-season/unavailable)", path, e.response.status_code)
        else:
            logger.error("OddsAPI error %s: %s", path, e)
        return None
    except httpx.HTTPError as e:
        logger.error("OddsAPI error %s: %s", path, e)
        return None


def fetch_events(sport_key: str) -> list[dict]:
    result = _get(f"/sports/{sport_key}/odds", {
        "regions":    "us",
        "markets":    ",".join(MARKETS),
        "oddsFormat": "american",
        "bookmakers": ",".join(SPORTSBOOKS),
    })
    return result or []


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
        "tennis_atp_french_open":       _TENNIS_MARKETS,
        "tennis_wta_french_open":       _TENNIS_MARKETS,
        "mma_mixed_martial_arts":       _MMA_FULL,
        "boxing_boxing":                _MMA_FULL,
        "soccer_usa_nwsl":              _SOCCER_MARKETS + _TEAM_SOCCER + _GAME_SOCCER,
        "soccer_fifa_womens_world_cup": _SOCCER_MARKETS + _TEAM_SOCCER + _GAME_SOCCER,
        # All other soccer leagues
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
        book = bk["key"]
        for mkt in bk.get("markets", []):
            mkt_key    = mkt["key"]
            is_team    = mkt_key.startswith(_TEAM_MARKET_PREFIXES)
            stat       = mkt_key.replace("player_", "").replace("team_", "").replace("_", " ").title()
            for outcome in mkt.get("outcomes", []):
                player     = outcome.get("description", outcome.get("name", ""))
                direction  = outcome.get("name", "").lower()  # "Over" or "Under"
                line       = outcome.get("point")
                try:
                    odds = int(outcome.get("price", -110))
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
            return set(json.loads(cached))

        # Cache miss — fetch from Sofascore
        from src.apis.sofascore import get_active_sports_today, SPORT_MAP
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
    from datetime import timezone, timedelta
    from dateutil.parser import parse as _parse
    from zoneinfo import ZoneInfo

    cutoff = datetime.now(ZoneInfo("America/New_York")).astimezone(timezone.utc) + timedelta(hours=24)
    tasks  = []
    for sport_key, events in all_events.items():
        if sport_key not in PLAYER_PROP_SPORTS:
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
                    # Use astimezone (not .replace) to correctly convert tz-aware datetimes
                    ct_utc = ct.astimezone(timezone.utc) if ct.tzinfo else ct.replace(tzinfo=timezone.utc)
                    if ct_utc > cutoff:
                        continue
            except Exception:
                pass
            tasks.append((sport_key, ev["id"]))

    if not tasks:
        return []

    all_props = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_player_props, sk, eid): (sk, eid) for sk, eid in tasks}
        for fut in futures:
            try:
                all_props.extend(fut.result())
            except Exception as e:
                logger.warning("Player props fetch failed: %s", e)

    logger.info("OddsAPI player props total: %d across %d events", len(all_props), len(tasks))
    return all_props


# ── Normalised data structures ─────────────────────────────────────────────────

def normalise_event(event: dict, sport_key: str) -> dict:
    """Flatten a raw Odds API event into a consistent structure."""
    markets: dict = {}
    for bk in event.get("bookmakers", []):
        book = bk["key"]
        for mkt in bk.get("markets", []):
            mk = mkt["key"]
            markets.setdefault(mk, {})
            for outcome in mkt.get("outcomes", []):
                sel   = outcome["name"]
                try:
                    price = int(outcome.get("price", -110))
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

def scan_all_sports() -> dict[str, list[dict]]:
    """
    Scan ALL sports on Odds API — every sport, every league, men's and women's.
    No filtering, no gating. Sofascore already told us what's playing at 8 AM;
    we don't re-check it here. We just pull every line available.
    """
    sport_keys = set(SPORTS.values())
    logger.info("OddsAPI scanning %d sport keys...", len(sport_keys))
    result: dict[str, list[dict]] = {}
    for sport_key in sport_keys:
        events = fetch_events(sport_key)
        if events:
            result[sport_key] = [normalise_event(e, sport_key) for e in events]
            logger.info("Odds: %d events for %s", len(events), sport_key)
    if not result:
        logger.warning("OddsAPI: 0 events across ALL %d sports — check API key quota and sport availability", len(sport_keys))
    return result


def save_snapshots_to_db(all_events: dict[str, list[dict]]) -> None:
    """Persist all current odds as OddsSnapshots. Upserts Game records on the fly."""
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from dateutil.parser import parse as _parse
    from datetime import timezone
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
                        ct = datetime.now(_ZoneInfo("America/New_York")).astimezone(timezone.utc).replace(tzinfo=None)
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
    """Scan all sports, save to DB, return flat list of snapshots."""
    all_events = scan_all_sports()
    save_snapshots_to_db(all_events)
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
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    from src.core.timezone import et_naive as _et_naive

    _ET = ZoneInfo("America/New_York")
    # All time math anchored to ET.
    # commence_time is stored as naive UTC in the DB, so we convert ET→UTC for filters.
    now_et_aware = datetime.now(_ET)
    now_et       = now_et_aware.replace(tzinfo=None)          # naive ET (matches captured_at column)
    now_utc      = now_et_aware.astimezone(timezone.utc).replace(tzinfo=None)   # naive UTC (matches commence_time column)

    # Snapshot freshness: captured within last 6 ET hours
    cutoff_lo = now_et - timedelta(hours=6)

    # Game window: ET end-of-tomorrow (so the full day+night slate is always covered)
    et_eod = now_et_aware.replace(hour=23, minute=59, second=59) + timedelta(days=1)
    cutoff_hi = et_eod.astimezone(timezone.utc).replace(tzinfo=None)

    result: dict[int, list[dict]] = {}
    with get_db() as db:
        rows = (
            db.query(OddsSnapshot, Game)
            .join(Game, Game.id == OddsSnapshot.game_id)
            .filter(
                OddsSnapshot.captured_at >= cutoff_lo,
                Game.commence_time != None,
                Game.commence_time >= now_utc,    # not already started (ET-anchored, stored as UTC)
                Game.commence_time <  cutoff_hi,  # within ET end-of-tomorrow
            )
            .all()
        )
        for snap, game in rows:
            # commence_time stored as naive UTC — tag it so parsers can convert to ET correctly
            ct = game.commence_time
            ct_str = (ct.strftime("%Y-%m-%dT%H:%M:%SZ") if ct else "")
            result.setdefault(snap.game_id, []).append({
                "book":          snap.book,
                "market":        snap.market,
                "selection":     snap.selection,
                "best_odds":     snap.american_odds,
                "decimal_odds":  snap.decimal_odds,
                "line_value":    snap.line_value,
                "sport_key":     game.sport,
                "home_team":     game.home_team,
                "away_team":     game.away_team,
                "commence_time": ct_str,
            })
    return result
