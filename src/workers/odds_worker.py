"""Odds worker — scans all sports and persists snapshots."""
import logging
from src.workers.celery_app import app
from src.engines.odds_engine import run_full_odds_scan, get_latest_snapshots_by_game

logger = logging.getLogger(__name__)


def _is_sleep_time() -> bool:
    """True when Eastern time is between 3 AM and 5 AM (sleep window)."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


@app.task(bind=True, max_retries=3, default_retry_delay=30)
def scan_and_save_odds(self):
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
                cutoff = datetime.utcnow() - timedelta(hours=2)
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
            send_line_movement_alerts.delay([dataclasses.asdict(m) for m in all_movements])

        return {"snapshots": len(snapshots), "movements": len(all_movements)}
    except Exception as exc:
        logger.error("Odds scan failed: %s", exc)
        raise self.retry(exc=exc)


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
    send_pick_line_update.delay(relevant)


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


@app.task(bind=True, max_retries=2, default_retry_delay=60)
def scan_player_props(self):
    """
    Scan betting apps for live odds, props, and markets every 10 min.

    Sources:
      PrizePicks  — player Over/Under props (public, no key)
      Underdog    — player Over/Under props (public, no key)
      HardRock    — ML/spread/totals via Odds API (already in scan_and_save_odds)
      Kalshi      — prediction market contracts (KALSHI_API_KEY_ID required)

    Results cached in Redis (TTL 15 min) for picks_worker to read.
    """
    if _is_sleep_time():
        logger.debug("scan_player_props: sleep window active, skipping")
        return {"skipped": "sleep_mode"}
    try:
        from src.apis.prizepicks import get_all_projections
        from src.apis.underdog import get_all_lines
        from src.apis.kalshi import get_sports_markets as kalshi_markets
        from src.apis.polymarket import get_sports_markets as polymarket_markets
        from src.core.config import REDIS_URL
        import redis as _redis
        import json
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict = {}
        prop_tasks = {
            "prizepicks": get_all_projections,
            "underdog":   get_all_lines,
        }

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(fn): name for name, fn in prop_tasks.items()}
            # Kalshi + Polymarket run in parallel, stored separately
            kalshi_future     = pool.submit(kalshi_markets)
            polymarket_future = pool.submit(polymarket_markets)
            for future in as_completed(futures, timeout=30):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    logger.warning("Props scan [%s] failed: %s", name, e)
                    results[name] = []
            try:
                kalshi_result = kalshi_future.result(timeout=15)
            except Exception as e:
                logger.warning("Kalshi scan failed: %s", e)
                kalshi_result = []
            try:
                polymarket_result = polymarket_future.result(timeout=15)
            except Exception as e:
                logger.warning("Polymarket scan failed: %s", e)
                polymarket_result = []

        all_props = []
        for items in results.values():
            all_props.extend(items or [])

        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # ── Detect changes vs previous snapshot ───────────────────────────────
        all_changes: list[dict] = []
        for source_name, new_items in results.items():
            prev_raw = r.get(f"props:{source_name}")
            prev_items: list[dict] = json.loads(prev_raw) if prev_raw else []
            changes = _detect_prop_changes(prev_items, new_items or [], source_name)
            all_changes.extend(changes)

        # Cache prop snapshots (PP + Underdog only in props:all)
        for name, items in results.items():
            r.setex(f"props:{name}", 900, json.dumps(items or []))
        r.setex("props:all", 900, json.dumps(all_props))

        # Cache Kalshi + Polymarket separately — picked up by picks_worker for AI scoring
        if kalshi_result:
            r.setex("kalshi:markets", 900, json.dumps(kalshi_result))
        if polymarket_result:
            r.setex("polymarket:markets", 900, json.dumps(polymarket_result))

        # Only alert on changes to picks we already recommended
        if all_changes:
            logger.info("Props changed: %d updates (checking against active picks)", len(all_changes))
            _alert_active_pick_changes(r, all_changes)

        counts = {k: len(v or []) for k, v in results.items()}
        counts["kalshi"]     = len(kalshi_result or [])
        counts["polymarket"] = len(polymarket_result or [])
        logger.info("Props scan complete: %s | total=%d", counts, len(all_props))
        return {**counts, "total": len(all_props), "changes": len(all_changes)}

    except Exception as exc:
        logger.error("Props scan failed: %s", exc)
        raise self.retry(exc=exc)
