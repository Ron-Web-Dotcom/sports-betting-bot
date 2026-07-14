"""Odds worker — scans all sports and persists snapshots."""
import logging
from src.engines.odds_engine import run_full_odds_scan

logger = logging.getLogger(__name__)


def _is_sleep_time() -> bool:
    """True when Eastern time is between 3 AM and 5 AM (sleep window)."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


def _odds_window() -> bool:
    """Odds scan runs 5 AM–3 AM ET only. Matches sleep window (3–5 AM ET)."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return not (3 <= et.hour < 5)


def scan_and_save_odds():
    from dataclasses import asdict
    if not _odds_window():
        logger.debug("scan_and_save_odds: sleep hours (3–5 AM ET), skipping")
        return {"skipped": "dead_hours"}
    try:
        snapshots = run_full_odds_scan()
        # Serialise any dataclass snapshots to plain dicts for downstream consumers
        serialised = [
            asdict(s) if hasattr(s, "__dataclass_fields__") else s
            for s in snapshots
        ]
        logger.info("Odds scan complete: %d events", len(serialised))
        return {"snapshots": len(serialised)}
    except Exception as exc:
        logger.error("Odds scan failed: %s", exc)
        raise


def _prop_key(prop: dict) -> str:
    """Unique key for a prop: player/subject + stat + sport."""
    subject = prop.get('player') or prop.get('subject', '')
    return f"{subject}|{prop.get('stat', '')}|{prop.get('sport_key', '')}"


def _alert_active_pick_changes(r, all_changes: list[dict]):
    """
    Check if any of our recommended picks moved or went off-board.
    Reads active picks from the same Redis key that picks_worker writes.
    """
    import json
    active_raw = r.get("props:odds_api")
    if not active_raw:
        return
    try:
        active_picks = json.loads(active_raw)
    except Exception:
        return

    active_keys = {
        f"{p.get('player', p.get('subject', ''))}|{p.get('stat', '')}|{p.get('sport_key', '')}": p
        for p in active_picks
    }
    if not active_keys:
        return

    relevant = []
    for c in all_changes:
        key = f"{c.get('player') or c.get('subject', '')}|{c.get('stat', '')}|{c.get('sport_key', '')}"
        if key in active_keys:
            pick = active_keys[key]
            relevant.append({**c, "our_direction": pick.get("direction", "")})

    if not relevant:
        return

    try:
        from src.workers.alert_worker import send_pick_line_update
        send_pick_line_update(relevant)
    except Exception as e:
        logger.warning("send_pick_line_update failed: %s", e)


def _detect_prop_changes(prev: list[dict], curr: list[dict], source: str) -> list[dict]:
    """Compare two prop snapshots and return a list of change dicts."""
    prev_map = {_prop_key(p): p for p in prev}
    curr_map = {_prop_key(p): p for p in curr}

    changes = []

    for key, new_prop in curr_map.items():
        old_prop = prev_map.get(key)
        if old_prop is None:
            changes.append({
                "change_type": "added",
                "source":      source,
                "subject":     new_prop.get("player") or new_prop.get("subject"),
                "stat":        new_prop.get("stat"),
                "sport_key":   new_prop.get("sport_key"),
                "new_line":    new_prop.get("line"),
                "old_line":    None,
            })
        elif old_prop.get("line") != new_prop.get("line"):
            changes.append({
                "change_type": "moved",
                "source":      source,
                "subject":     new_prop.get("player") or new_prop.get("subject"),
                "stat":        new_prop.get("stat"),
                "sport_key":   new_prop.get("sport_key"),
                "old_line":    old_prop.get("line"),
                "new_line":    new_prop.get("line"),
            })

    for key, old_prop in prev_map.items():
        if key not in curr_map:
            changes.append({
                "change_type": "removed",
                "source":      source,
                "subject":     old_prop.get("player") or old_prop.get("subject"),
                "stat":        old_prop.get("stat"),
                "sport_key":   old_prop.get("sport_key"),
                "old_line":    old_prop.get("line"),
                "new_line":    None,
            })

    return changes


def refresh_active_sports():
    """
    Wipe ALL stale sport/game caches so every morning starts with fresh data.
    Runs at 5:30 AM ET daily — before the 8 AM scan.
    """
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        # Clear everything that could carry yesterday's data into today's scans
        stale_keys = [
            "sofascore:active_sports",
            "oddsapi:active_sport_keys",   # 6h cache — could be from yesterday evening
            "sofascore:day_games",
            "sofascore:night_games",
            "sofascore:today_events",
            "sofascore:today_index",
            "props:odds_api",
            "props:all",
        ]
        deleted = r.delete(*stale_keys)
        logger.info("Morning cache wipe: deleted %d stale keys", deleted)
        # Eagerly re-populate the Odds API active sports list so the 8 AM scan is instant
        from src.engines.odds_engine import get_live_active_sport_keys
        active = get_live_active_sport_keys()
        logger.info("Active sports refreshed: %d sports", len(active))
        return {"deleted_keys": deleted, "active_sports": len(active)}
    except Exception as e:
        logger.error("refresh_active_sports failed: %s", e)
        return {"error": str(e)}


def _props_window() -> bool:
    """Props are only posted by bookmakers between 8 AM and 11 PM ET.
    No point scanning outside that window — saves API credits."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 8 <= et.hour < 23


def scan_player_props():
    """
    Scan all prop and market sources.

    Sources:
      Odds API   — ML, spreads, totals, player props (primary — all sports)
      Kalshi     — prediction market contracts
      PrizePicks — DISABLED
      Underdog   — DISABLED

    Results cached in Redis for picks_worker.
    """
    if not _props_window():
        logger.debug("scan_player_props: outside props window (8 AM–11 PM ET), skipping")
        return {"skipped": "outside_props_window"}
    if _is_sleep_time():
        logger.debug("scan_player_props: sleep window active, skipping")
        return {"skipped": "sleep_mode"}
    try:
        from src.engines.odds_engine import fetch_all_player_props, scan_all_sports
        from src.core.config import REDIS_URL
        import redis as _redis
        import json

        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # Fetch Odds API player props (team + individual)
        odds_props = []
        try:
            all_events = scan_all_sports()
            odds_props = fetch_all_player_props(all_events)
        except Exception as e:
            logger.warning("Odds API player props failed: %s", e)

        # Fetch Kalshi live sports events (player props, game props, totals, BTTS, spreads)
        kalshi_markets = []
        try:
            from src.apis.kalshi import get_sports_events
            kalshi_markets = get_sports_events(limit=200)
            r.setex("kalshi:live_markets", 2400, json.dumps(kalshi_markets))
            logger.info("Kalshi live markets cached: %d sub-markets", len(kalshi_markets))
        except Exception as e:
            logger.warning("Kalshi live scan failed: %s", e)

        # Detect changes
        prev_raw = r.get("props:odds_api")
        prev_props: list[dict] = json.loads(prev_raw) if prev_raw else []
        all_changes = _detect_prop_changes(prev_props, odds_props, "odds_api")

        # Cache
        r.setex("props:odds_api", 2400, json.dumps(odds_props))
        r.setex("props:all",      2400, json.dumps(odds_props + kalshi_markets))

        if all_changes:
            logger.info("Props changed: %d updates (checking against active picks)", len(all_changes))
            _alert_active_pick_changes(r, all_changes)

        logger.info("Props scan complete: odds_api=%d kalshi=%d | changes=%d",
                    len(odds_props), len(kalshi_markets), len(all_changes))
        return {"odds_api": len(odds_props), "kalshi": len(kalshi_markets),
                "total": len(odds_props), "changes": len(all_changes)}

    except Exception as exc:
        logger.error("Props scan failed: %s", exc)
        raise
