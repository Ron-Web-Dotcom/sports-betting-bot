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
from src.engines.ev_engine import american_to_decimal, implied_prob

logger = logging.getLogger(__name__)

MARKETS = ["h2h", "spreads", "totals"]


# ── Raw API calls ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict | list | None:
    params["apiKey"] = ODDS_API_KEY
    try:
        r = httpx.get(f"{ODDS_API_BASE}{path}", params=params, timeout=20)
        r.raise_for_status()
        logger.debug("OddsAPI %s remaining=%s", path, r.headers.get("x-requests-remaining"))
        return r.json()
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
    """
    result: dict[str, list[dict]] = {}
    for short_name, sport_key in SPORTS.items():
        events = fetch_events(sport_key)
        if events:
            result[sport_key] = [normalise_event(e, sport_key) for e in events]
            logger.info("Odds: %d events for %s", len(events), sport_key)
    return result


def save_snapshots_to_db(all_events: dict[str, list[dict]]) -> None:
    """Persist all current odds as OddsSnapshots for line-movement tracking."""
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, Game

    with get_db() as db:
        for sport_key, events in all_events.items():
            for event in events:
                game = db.query(Game).filter_by(external_id=event["id"]).first()
                if not game:
                    continue
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
    from datetime import datetime, timedelta

    result: dict[int, list[dict]] = {}
    with get_db() as db:
        rows = (
            db.query(OddsSnapshot, Game)
            .join(Game, Game.id == OddsSnapshot.game_id)
            .filter(OddsSnapshot.captured_at >= datetime.utcnow() - timedelta(hours=2))
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
