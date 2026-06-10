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
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_pass_tds", "player_pass_yds",
    "player_rush_yds", "player_reception_yds", "player_receptions",
    "player_hits", "player_total_bases", "player_strikeouts",
    "player_shots_on_target",
]

# Soccer leagues — share the same prop markets
_SOCCER_LEAGUES = {
    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_usa_mls",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_fifa_world_cup",
    "soccer_conmebol_copa_america", "soccer_conmebol_copa_libertadores",
}

# All sports that support player props on Odds API
PLAYER_PROP_SPORTS = {
    "basketball_nba", "basketball_wnba", "basketball_ncaab", "basketball_wncaab",
    "americanfootball_nfl", "americanfootball_ncaaf",
    "baseball_mlb",
    "icehockey_nhl",
    "tennis_atp_french_open", "tennis_wta_french_open",
    "mma_mixed_martial_arts",
} | _SOCCER_LEAGUES


# ── Raw API calls ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict | list | None:
    params["apiKey"] = ODDS_API_KEY
    try:
        r = httpx.get(f"{ODDS_API_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        logger.debug("OddsAPI %s remaining=%s", path, r.headers.get("x-requests-remaining"))
        return r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            logger.debug("OddsAPI %s → %s (off-season or unsupported market)", path, e.response.status_code)
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
    _sport_markets = {
        "basketball_nba":               _BASKETBALL_MARKETS,
        "basketball_wnba":              _BASKETBALL_MARKETS,
        "basketball_ncaab":             ["player_points", "player_rebounds", "player_assists"],
        "basketball_wncaab":            ["player_points", "player_rebounds", "player_assists"],
        "americanfootball_nfl":         ["player_pass_tds", "player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
        "americanfootball_ncaaf":       ["player_pass_tds", "player_pass_yds", "player_rush_yds", "player_reception_yds"],
        "baseball_mlb":                 ["batter_home_runs", "batter_hits", "batter_total_bases", "pitcher_strikeouts"],
        "icehockey_nhl":                ["player_shots_on_target", "player_points", "player_goals"],
        "tennis_atp_french_open":       _TENNIS_MARKETS,
        "tennis_wta_french_open":       _TENNIS_MARKETS,
        "mma_mixed_martial_arts":       ["player_method_of_victory", "player_total_rounds"],
        "soccer_usa_nwsl":              _SOCCER_MARKETS,
        "soccer_fifa_womens_world_cup": _SOCCER_MARKETS,
        # All other soccer leagues
        **{league: _SOCCER_MARKETS for league in _SOCCER_LEAGUES},
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

    props = []
    for bk in data.get("bookmakers", []):
        book = bk["key"]
        for mkt in bk.get("markets", []):
            stat = mkt["key"].replace("player_", "").replace("_", " ").title()
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
                        "player":      player,
                        "stat":        stat,
                        "line":        line,
                        "over_odds":   {},   # book -> odds
                        "under_odds":  {},
                        "sport_key":   sport_key,
                        "event_id":    event_id,
                        "source":      "odds_api",
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

        # Expand: if a sport_key is active, also mark all its player-prop variants active.
        # e.g. basketball_nba active → basketball_wnba, basketball_ncaab also included
        # because Sofascore checks by slug ("basketball") which covers all variants.
        from src.apis.sofascore import SPORT_MAP as _SM
        slug_to_keys: dict[str, list[str]] = {}
        for sk, slug in _SM.items():
            slug_to_keys.setdefault(slug, []).append(sk)

        active: set[str] = set(active_from_sofascore)
        for sk in active_from_sofascore:
            slug = _SM.get(sk)
            if slug:
                active.update(slug_to_keys.get(slug, []))

        # Also include ALL PLAYER_PROP_SPORTS that share a slug with any active sport
        for sk in list(PLAYER_PROP_SPORTS):
            slug = _SM.get(sk)
            if slug and any(_SM.get(a) == slug for a in active_from_sofascore):
                active.add(sk)

        # TTL = seconds until midnight ET
        now_et = et_naive()
        from datetime import datetime as _dt, timedelta
        midnight = _dt.combine(now_et.date(), _dt.min.time()) + timedelta(days=1)
        ttl = max(int((midnight - now_et).total_seconds()), 3600)
        r.setex("sofascore:active_sports", ttl, json.dumps(list(active)))
        logger.info("Active sports cached (%d): %s", len(active), sorted(active))
        return active
    except Exception as e:
        logger.warning("Active sports cache failed: %s — defaulting to all sports", e)
        return set(PLAYER_PROP_SPORTS)  # fail open


def fetch_all_player_props(all_events: dict[str, list[dict]]) -> list[dict]:
    """
    Fetch player props only for sports Sofascore confirms have games today.
    Active sports are cached in Redis until midnight — Sofascore is only
    called once per day, not on every props scan.
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timezone, timedelta
    from dateutil.parser import parse as _parse

    cutoff = datetime.now(timezone.utc) + timedelta(hours=24)
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
    Fetch and normalise all events across all tracked sports.
    Returns {sport_key: [normalised_event, ...]}
    Deduplicates sport_keys so each league is fetched exactly once.
    """
    result: dict[str, list[dict]] = {}
    for sport_key in set(SPORTS.values()):  # deduplicate aliases like soccer/epl → soccer_epl
        events = fetch_events(sport_key)
        if events:
            result[sport_key] = [normalise_event(e, sport_key) for e in events]
            logger.info("Odds: %d events for %s", len(events), sport_key)
    return result


def save_snapshots_to_db(all_events: dict[str, list[dict]]) -> None:
    """Persist all current odds as OddsSnapshots. Upserts Game records on the fly."""
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from dateutil.parser import parse as _parse
    from datetime import timezone

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
                        ct = datetime.utcnow()
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
    """Return {game_id: [snapshot_dicts]} for all open games.

    Each dict includes game-level fields (sport_key, home_team, away_team,
    commence_time) so callers don't need a separate Game lookup.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game
    from datetime import timedelta

    result: dict[int, list[dict]] = {}
    with get_db() as db:
        rows = (
            db.query(OddsSnapshot, Game)
            .join(Game, Game.id == OddsSnapshot.game_id)
            .filter(OddsSnapshot.captured_at >= et_naive() - timedelta(hours=2))
            .all()
        )
        for snap, game in rows:
            result.setdefault(snap.game_id, []).append({
                "book":          snap.book,
                "market":        snap.market,
                "selection":     snap.selection,
                "best_odds":     snap.american_odds,
                "decimal_odds":  snap.decimal_odds,
                # game-level fields needed by picks_worker
                "sport_key":     game.sport,
                "home_team":     game.home_team,
                "away_team":     game.away_team,
                "commence_time": str(game.commence_time or ""),
            })
    return result
