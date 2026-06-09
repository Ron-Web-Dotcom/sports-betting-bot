"""Odds worker — scans all sports and persists snapshots."""
import logging
from src.engines.odds_engine import run_full_odds_scan, get_latest_snapshots_by_game
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)


def _is_sleep_time() -> bool:
    """True when Eastern time is between 3 AM and 5 AM (sleep window)."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


def scan_and_save_odds():
    if _is_sleep_time():
        logger.debug("scan_and_save_odds: sleep window active, skipping")
        return {"skipped": "sleep_mode"}
    try:
        snapshots = run_full_odds_scan()
        logger.info("Odds scan complete: %d events", len(snapshots))

        # Detect line movements per game using DB snapshots (require captured_at for ordering)
        all_movements = []
        try:
            from src.engines.line_movement_engine import detect_movements, save_movement
            from src.db.session import get_db
            from src.db.models import OddsSnapshot, Game
            from datetime import datetime, timedelta

            # Group snapshots by game_id inside session to avoid DetachedInstanceError
            by_game: dict[int, dict] = {}
            with get_db() as db:
                cutoff = et_naive() - timedelta(hours=2)
                recent = (
                    db.query(OddsSnapshot, Game.external_id, Game.home_team, Game.away_team)
                    .join(Game, OddsSnapshot.game_id == Game.id)
                    .filter(OddsSnapshot.captured_at >= cutoff)
                    .all()
                )
                for snap, ext_id, home, away in recent:
                    gid = snap.game_id
                    if gid not in by_game:
                        by_game[gid] = {
                            "event_name": f"{away} vs {home}",
                            "snaps": [],
                        }
                    by_game[gid]["snaps"].append({
                        "market":        snap.market,
                        "selection":     snap.selection,
                        "book":          snap.book,
                        "american_odds": snap.american_odds,
                        "captured_at":   snap.captured_at,
                    })

            for game_id, info in by_game.items():
                try:
                    movements = detect_movements(
                        game_id          = game_id,
                        event_name       = info["event_name"],
                        current_snapshots= info["snaps"],
                    )
                    for mv in movements:
                        save_movement(mv)
                    all_movements.extend(movements)
                except Exception as game_exc:
                    logger.error("Movement detection failed for game %s: %s", game_id, game_exc)

        except Exception as mv_exc:
            logger.warning("Line movement detection failed: %s", mv_exc)

        if all_movements:
            from src.workers.alert_worker import send_line_movement_alerts
            import dataclasses
            send_line_movement_alerts([dataclasses.asdict(m) for m in all_movements])

        return {"snapshots": len(snapshots), "movements": len(all_movements)}
    except Exception as exc:
        logger.error("Odds scan failed: %s", exc)
        raise


def _prop_key(prop: dict) -> str:
    """Unique key for a prop: subject + stat + sport."""
    return f"{prop.get('subject', '')}|{prop.get('stat', '')}|{prop.get('sport_key', '')}"


def _alert_active_pick_changes(r, all_changes: list[dict]):
    """
    Check if any of our recommended picks moved or went off-board.
    Post a brief Discord update only for those — not for all 2000+ props.
    """
    import json
    last_raw = r.get("props:last_picks_hash")
    if not last_raw:
        return  # no active picks to track

    active_raw = r.get("props:active_picks")
    if not active_raw:
        return
    active_picks = json.loads(active_raw)
    active_keys = {
        f"{p.get('subject', '')}|{p.get('stat', '')}|{p.get('sport_key', '')}": p
        for p in active_picks
    }

    relevant = []
    for c in all_changes:
        key = f"{c.get('subject', '')}|{c.get('stat', '')}|{c.get('sport_key', '')}"
        if key in active_keys:
            relevant.append({**c, "our_direction": active_picks[0].get("direction", "")})

    if not relevant:
        return

    from src.workers.alert_worker import send_pick_line_update
    send_pick_line_update(relevant)


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
                "subject":     new_prop.get("subject"),
                "stat":        new_prop.get("stat"),
                "sport_key":   new_prop.get("sport_key"),
                "new_line":    new_prop.get("line"),
                "old_line":    None,
            })
        elif old_prop.get("line") != new_prop.get("line"):
            changes.append({
                "change_type": "moved",
                "source":      source,
                "subject":     new_prop.get("subject"),
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
                "subject":     old_prop.get("subject"),
                "stat":        old_prop.get("stat"),
                "sport_key":   old_prop.get("sport_key"),
                "old_line":    old_prop.get("line"),
                "new_line":    None,
            })

    return changes


def refresh_active_sports():
    """
    Bust the Sofascore active-sports cache so the next props scan
    re-checks what leagues have games today. Runs at 5:30 AM ET daily.
    """
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        r.delete("sofascore:active_sports")
        # Eagerly re-populate so first scan of the day is instant
        from src.engines.odds_engine import _get_active_sports_cached
        active = _get_active_sports_cached()
        logger.info("Active sports refreshed: %s", sorted(active))
        return {"active_sports": sorted(active)}
    except Exception as e:
        logger.error("refresh_active_sports failed: %s", e)
        return {"error": str(e)}


def scan_player_props():
    """
    Scan all prop and market sources.

    Sources:
      Odds API   — ML, spreads, totals, player props (primary — all sports)
      Kalshi     — prediction market contracts
      Polymarket — prediction markets
      PrizePicks — DISABLED
      Underdog   — DISABLED

    Results cached in Redis for picks_worker.
    """
    if _is_sleep_time():
        logger.debug("scan_player_props: sleep window active, skipping")
        return {"skipped": "sleep_mode"}
    try:
        from src.apis.kalshi import get_sports_markets as kalshi_markets
        from src.apis.polymarket import get_sports_markets as polymarket_markets
        from src.engines.odds_engine import fetch_all_player_props, scan_all_sports
        from src.core.config import REDIS_URL
        import redis as _redis
        import json
        from concurrent.futures import ThreadPoolExecutor

        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # Fetch Odds API player props
        odds_props = []
        all_events = {}
        try:
            all_events = scan_all_sports()
            odds_props = fetch_all_player_props(all_events)
        except Exception as e:
            logger.warning("Odds API player props failed: %s", e)

        # Kalshi + Polymarket in parallel
        kalshi_result, polymarket_result = [], []
        with ThreadPoolExecutor(max_workers=2) as pool:
            kf = pool.submit(kalshi_markets)
            pf = pool.submit(polymarket_markets)
            try:
                kalshi_result = kf.result(timeout=15)
            except Exception as e:
                logger.warning("Kalshi scan failed: %s", e)
            try:
                polymarket_result = pf.result(timeout=15)
            except Exception as e:
                logger.warning("Polymarket scan failed: %s", e)

        # Detect changes on Odds API props
        all_changes: list[dict] = []
        prev_raw = r.get("props:odds_api")
        prev_props: list[dict] = json.loads(prev_raw) if prev_raw else []
        all_changes = _detect_prop_changes(prev_props, odds_props, "odds_api")

        # Cache everything
        r.setex("props:odds_api", 1500, json.dumps(odds_props))
        r.setex("props:all",      1500, json.dumps(odds_props))
        if kalshi_result:
            r.setex("kalshi:markets",    1500, json.dumps(kalshi_result))
        if polymarket_result:
            r.setex("polymarket:markets", 1500, json.dumps(polymarket_result))

        if all_changes:
            logger.info("Props changed: %d updates (checking against active picks)", len(all_changes))
            _alert_active_pick_changes(r, all_changes)

        counts = {
            "odds_api":   len(odds_props),
            "kalshi":     len(kalshi_result),
            "polymarket": len(polymarket_result),
        }
        logger.info("Props scan complete: %s | total=%d", counts, len(odds_props))
        return {**counts, "total": len(odds_props), "changes": len(all_changes)}

    except Exception as exc:
        logger.error("Props scan failed: %s", exc)
        raise
