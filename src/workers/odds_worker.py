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
            "oddsapi:active_sport_keys",   # must bust so new API key takes effect immediately
            "sofascore:day_games",
            "sofascore:night_games",
            "sofascore:today_events",
            "sofascore:today_index",
            "props:odds_api",
            "props:all",
            "kalshi:live_markets",         # bust Kalshi cache too
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
            kalshi_markets = get_sports_events(limit=500)
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

        # Build enriched game cache for picks_worker + prediction_market_worker
        try:
            build_enriched_games_cache()
        except Exception as e:
            logger.warning("build_enriched_games_cache failed: %s", e)

        return {"odds_api": len(odds_props), "kalshi": len(kalshi_markets),
                "total": len(odds_props), "changes": len(all_changes)}

    except Exception as exc:
        logger.error("Props scan failed: %s", exc)
        raise


def build_enriched_games_cache():
    """
    Build games:enriched — one entry per game, structured exactly like the
    getlive_gamestoday.py tables so picks_worker and prediction_market_worker
    see the same clean data the operator sees on screen.

    Each game entry mirrors the 4 tables:
      TABLE 1  sofascore_status, sofascore_id, opens_et, period
      TABLE 2  matchup, sport, time_et, away_odds, home_odds, bot_pick,
               bot_pick_odds, other_odds, confidence
      TABLE 3  props  [ {player, stat, line, over_odds, under_odds} ]
      TABLE 4  kalshi [ {title, yes_c, no_c, pick, volume, game_start_et} ]
               kalshi_agrees  — True when best Kalshi pick matches bot_pick
    """
    import json
    import re as _re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.core.config import REDIS_URL
    import redis as _redis

    r   = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    ET  = ZoneInfo("America/New_York")

    # ── helpers ──────────────────────────────────────────────────────────────
    _SUF = _re.compile(r'\b(fc|city|united|sc|cf|afc|bfc|sporting|athletics)\b', _re.IGNORECASE)
    def _norm(name: str) -> str:
        if not name: return ""
        return _re.sub(r'\s+', ' ', _SUF.sub("", name).strip()).lower()

    def _fmt_odds(o) -> str:
        if o is None: return "N/A"
        return f"+{o}" if o > 0 else str(o)

    def _to_et_str(ct: str) -> str:
        if not ct: return ""
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return dt.astimezone(ET).strftime("%-I:%M %p ET")
        except Exception:
            return ""

    def _period(ct: str) -> str:
        if not ct: return "—"
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(ET)
            return "NIGHT" if dt.hour >= 16 else "DAY"
        except Exception:
            return "—"

    def _novig(p1_odds, p2_odds) -> float | None:
        if p1_odds is None: return None
        def _impl(o):
            if o is None: return 50.0
            return abs(o)/(abs(o)+100)*100 if o < 0 else 100/(100+o)*100
        p1 = _impl(p1_odds)
        p2 = _impl(p2_odds)
        tot = p1 + p2
        nv = round(p1/tot*100, 1) if tot > 0 else round(p1, 1)
        if p1_odds < 0:
            o = abs(p1_odds)
            if   o >= 1000: nv = max(nv, 97.0)
            elif o >=  700: nv = max(nv, 95.0)
            elif o >=  500: nv = max(nv, 93.0)
            elif o >=  350: nv = max(nv, 90.0)
            elif o >=  250: nv = max(nv, 86.0)
            elif o >=  200: nv = max(nv, 83.0)
            elif o >=  150: nv = max(nv, 65.0)
            elif o >=  110: nv = max(nv, 58.0)
        return nv

    # ── load sources ─────────────────────────────────────────────────────────
    sf_events  = json.loads(r.get("sofascore:today_events") or "[]")
    props_raw  = json.loads(r.get("props:odds_api") or "[]")
    kalshi_raw = json.loads(r.get("kalshi:live_markets") or "[]")

    from src.engines.odds_engine import get_latest_snapshots_by_game
    snaps = get_latest_snapshots_by_game()

    # ── TABLE 2 base: Odds API snapshots ─────────────────────────────────────
    enriched: dict[str, dict] = {}
    for game_id, snap_list in snaps.items():
        if not snap_list: continue
        s0   = snap_list[0]
        ct   = s0.get("commence_time", "")
        home = s0.get("home_team", "")
        away = s0.get("away_team", "")
        sport = s0.get("sport_key", "")

        # Build h2h odds map  {team_name: best_odds}
        h2h: dict[str, int] = {}
        for s in snap_list:
            if s.get("market") != "h2h": continue
            sel, odds = s.get("selection", ""), s.get("best_odds")
            if sel and odds is not None:
                if sel not in h2h or odds > h2h[sel]:
                    h2h[sel] = odds

        # Bot pick = team with most negative odds (strongest favorite)
        bot_pick = bot_pick_odds = other_odds = None
        if h2h:
            neg = {t: o for t, o in h2h.items() if o < 0}
            if neg:
                bot_pick      = min(neg, key=lambda t: neg[t])
                bot_pick_odds = h2h[bot_pick]
                other_odds    = next((o for t, o in h2h.items() if t != bot_pick), None)
            else:
                # Both positive — pick lower (less underdog)
                bot_pick      = min(h2h, key=lambda t: h2h[t])
                bot_pick_odds = h2h[bot_pick]
                other_odds    = next((o for t, o in h2h.items() if t != bot_pick), None)

        # Away / home odds for display
        away_odds = h2h.get(away) or next((o for t, o in h2h.items() if _norm(t) in _norm(away) or _norm(away) in _norm(t)), None)
        home_odds = h2h.get(home) or next((o for t, o in h2h.items() if _norm(t) in _norm(home) or _norm(home) in _norm(t)), None)

        conf = _novig(bot_pick_odds, other_odds)

        enriched[game_id] = {
            # ── identity ──────────────────────────────────────────────────
            "game_id":       game_id,
            "matchup":       f"{away} @ {home}",
            "away_team":     away,
            "home_team":     home,
            "sport":         sport,
            "time_et":       _to_et_str(ct),
            "period":        _period(ct),
            "commence_time": ct,

            # ── TABLE 2: odds ─────────────────────────────────────────────
            "away_odds":     _fmt_odds(away_odds),
            "home_odds":     _fmt_odds(home_odds),
            "bot_pick":      bot_pick or "—",
            "bot_pick_odds": _fmt_odds(bot_pick_odds),
            "other_odds":    _fmt_odds(other_odds),
            "confidence":    conf,          # no-vig % with magnitude floors
            "h2h_raw":       h2h,           # full {team: odds} for AI use

            # ── TABLE 1: Sofascore (filled below) ─────────────────────────
            "status":        "—",           # LIVE / SOON / Scheduled / 2nd half …
            "sofascore_id":  "",

            # ── TABLE 3: Props (filled below) ─────────────────────────────
            "props": [],                    # [{player, stat, line, over_odds, under_odds}]

            # ── TABLE 4: Kalshi (filled below) ────────────────────────────
            "kalshi":        [],            # [{title, yes_c, no_c, pick, volume, game_start_et}]
            "kalshi_agrees": False,         # True = Kalshi best pick matches bot_pick
            "kalshi_top_price": None,       # ¢ of the highest-confidence Kalshi market
        }

    # ── TABLE 1: merge Sofascore status ──────────────────────────────────────
    _LIVE_KW = {"live","inprogress","1h","2h","ht","et","pen","progress",
                "halftime","inning","quarter","period","set"}
    for ev in sf_events:
        eh = _norm(ev.get("home_team", ""))
        ea = _norm(ev.get("away_team", ""))
        for g in enriched.values():
            if _norm(g["home_team"]) != eh or _norm(g["away_team"]) != ea:
                continue
            raw = str(ev.get("status") or ev.get("status_type") or "").lower()
            if any(x in raw for x in _LIVE_KW):
                status = "🔴 LIVE"
            else:
                status = raw[:18] or "Scheduled"
            g["status"]       = status
            g["sofascore_id"] = str(ev.get("id", ""))
            break

    # ── TABLE 3: merge Props ──────────────────────────────────────────────────
    for prop in props_raw:
        ph = _norm(prop.get("home_team", ""))
        pa = _norm(prop.get("away_team", ""))
        for g in enriched.values():
            if ph and pa and ph == _norm(g["home_team"]) and pa == _norm(g["away_team"]):
                g["props"].append({
                    "player":     prop.get("player", "") or prop.get("subject", ""),
                    "stat":       prop.get("stat", ""),
                    "line":       prop.get("line"),
                    "over_odds":  prop.get("over_odds"),
                    "under_odds": prop.get("under_odds"),
                })
                break

    # ── TABLE 4: merge Kalshi markets ────────────────────────────────────────
    for m in kalshi_raw:
        title  = (m.get("title") or "").strip()
        sub    = (m.get("subtitle") or "").strip()
        t_low  = title.lower()
        yes_raw = m.get("yes_price") or 0
        no_raw  = m.get("no_price")  or 0
        yes_c = round(yes_raw * 100) if yes_raw <= 1 else round(yes_raw)
        no_c  = round(no_raw  * 100) if no_raw  <= 1 else round(no_raw)
        if yes_c == 0 and no_c == 0: continue
        pick_side   = "YES" if yes_c >= no_c else "NO"
        top_price   = max(yes_c, no_c)
        vol         = m.get("volume") or 0
        game_start  = _to_et_str(m.get("close_time") or m.get("expiration_time") or "")

        for g in enriched.values():
            ht_tok = _norm(g["home_team"]).split()[:1]
            at_tok = _norm(g["away_team"]).split()[:1]
            tokens = [t for t in (ht_tok + at_tok) if len(t) > 3]
            if not any(tok in t_low for tok in tokens):
                continue
            # Append this Kalshi market to the game's kalshi list
            g["kalshi"].append({
                "title":         f"{title} {sub}".strip()[:60],
                "market_id":     m.get("ticker") or m.get("market_id") or "",
                "event_ticker":  m.get("event_ticker") or "",
                "yes_c":         yes_c,
                "no_c":          no_c,
                "pick":          pick_side,
                "volume":        vol,
                "game_start_et": game_start,
            })
            # Update kalshi_agrees: check if Kalshi's pick side matches bot_pick
            if g["bot_pick"] and g["bot_pick"] != "—":
                bp_tok = _norm(g["bot_pick"]).split()[:1]
                if bp_tok and any(t in t_low for t in bp_tok):
                    if pick_side == "YES":
                        g["kalshi_agrees"] = True
                        if top_price > (g["kalshi_top_price"] or 0):
                            g["kalshi_top_price"] = top_price
            break

    # Sort each game's Kalshi markets by volume descending
    for g in enriched.values():
        g["kalshi"].sort(key=lambda x: x["volume"], reverse=True)

    out = list(enriched.values())
    # Only cache games that have odds (same filter as the display table)
    out = [g for g in out if g["h2h_raw"]]

    r.setex("games:enriched", 2400, json.dumps(out))
    logger.info(
        "games:enriched: %d games | %d LIVE | %d with Kalshi | %d with props | %d DAY / %d NIGHT",
        len(out),
        sum(1 for g in out if "LIVE" in g["status"]),
        sum(1 for g in out if g["kalshi"]),
        sum(1 for g in out if g["props"]),
        sum(1 for g in out if g["period"] == "DAY"),
        sum(1 for g in out if g["period"] == "NIGHT"),
    )
    return out
