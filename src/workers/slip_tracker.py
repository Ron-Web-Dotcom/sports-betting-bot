"""
Slip Tracker — tracks HardRock and Kalshi/Poly entry slips end-to-end.

When an entry is posted:
  → Save the slip (picks + game times) to Redis

Every 3 minutes:
  → Check each active slip's games
  → Fire "Game starts soon"  alert 30 min before kick-off
  → Fire "Game is LIVE now"  alert at kick-off
  → Fire "CASHED ✅ / DEAD ❌" alert when game result is known
  → Update W/L ratio

A slip is CASHED if every pick in it won.
A slip is DEAD if any pick lost (like a parlay — one loss kills the ticket).
"""
import json
import logging
from datetime import datetime, timedelta
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

_SLIP_KEY    = "slips:active"       # Redis hash: slip_id → slip JSON
_WEEKLY_KEY  = "slips:weekly"       # Redis hash: wins, losses, week_start (resets each Monday)

import re as _re
_SUFFIXES = _re.compile(
    r'\b(fc|city|united|sc|cf|afc|bfc|sporting|athletics)\b', _re.IGNORECASE
)

def _normalize_team_name(name: str) -> str:
    if not name:
        return ""
    name = _SUFFIXES.sub("", name).strip()
    return _re.sub(r'\s+', ' ', name).lower().strip()
_RATIO_KEY   = "slips:ratio"        # Redis hash: wins, losses (all-time)
_ALERTED_KEY = "slips:alerted"      # Redis set: {slip_id}:{event} already fired


def _redis():
    from src.core.config import REDIS_URL
    import redis as _r
    return _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


def _now_et() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))  # tz-aware ET


def _parse_time(ct: str) -> datetime | None:
    if not ct:
        return None
    try:
        from dateutil.parser import parse as _p
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        dt = _p(str(ct))
        if dt.tzinfo is None:
            # Already an ET naive string (Sofascore legacy or old saved slips)
            dt = dt.replace(tzinfo=_ET)
        return dt.astimezone(_ET)
    except Exception:
        return None


def _fmt_time(ct: str) -> str:
    import zoneinfo
    dt = _parse_time(ct)
    if not dt:
        return ""
    return dt.astimezone(zoneinfo.ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")


# ── Save slip ──────────────────────────────────────────────────────────────────

def purge_ghost_slips() -> int:
    """
    Remove stale slips from Redis whose IDs don't match the stable
    {period}:{platform}:{date} format (old hash-based IDs from prior runs).
    Keeps slips from the last 7 days so the weekly summary can count them;
    purges anything 8+ days old.
    Returns number of slips removed.
    """
    import re
    from src.core.timezone import et_naive
    r = _redis()
    all_slips = r.hgetall(_SLIP_KEY)
    _now      = et_naive()
    # Build set of date strings we want to keep (today + last 7 days)
    _keep_dates = {
        (_now - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(8)  # 0 = today … 7 = 7 days ago
    }
    removed   = 0
    for sid, raw in all_slips.items():
        # Valid IDs look like "day:hardrock:2026-06-13"
        if not re.match(r"^(day|night):[a-z]+:\d{4}-\d{2}-\d{2}$", sid):
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged ghost slip: %s", sid)
            continue
        slip_date = sid.split(":")[-1]
        if slip_date not in _keep_dates:
            # 8+ days old — safe to purge
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged old slip: %s", sid)
            continue
        # Void slips where any pick's game date doesn't match the slip's own date
        # (e.g. a slip created July 9 that picked a July 10 game — date gate miss)
        try:
            slip = json.loads(raw)
            if slip.get("status") == "active":
                from datetime import date as _date
                from dateutil.parser import parse as _dp_pg
                slip_day = _date.fromisoformat(slip_date)
                today    = _now.date()
                _wrong_date = False
                for _pk in slip.get("picks", []):
                    _ct = _pk.get("commence_time", "")
                    if _ct:
                        try:
                            _gd = _dp_pg(_ct)
                            from zoneinfo import ZoneInfo as _ZIpg
                            if _gd.tzinfo is None:
                                _gd = _gd.replace(tzinfo=_ZIpg("America/New_York"))
                            _game_date = _gd.astimezone(_ZIpg("America/New_York")).date()
                            if _game_date != slip_day:
                                _wrong_date = True
                                break
                        except Exception:
                            pass
                if _wrong_date:
                    slip["status"] = "void"
                    r.hset(_SLIP_KEY, sid, json.dumps(slip))
                    _db_save_slip(slip)
                    removed += 1
                    logger.warning("Voided slip %s — pick game date doesn't match slip date %s", sid, slip_date)
                    continue
                # Void slips where any pick's game had ALREADY STARTED when the slip was created.
                # This catches entries posted after a game kicked off (e.g. 4:35 PM entry for a 4:11 PM game).
                # Grace: only check slips created within 4h (fresh). Void if game started 30+ min before creation.
                try:
                    _created_str = slip.get("created", "")
                    if _created_str:
                        from zoneinfo import ZoneInfo as _ZIpg2
                        from dateutil.parser import parse as _dp_pg2
                        _slip_created = _dp_pg2(_created_str)
                        if _slip_created.tzinfo is None:
                            _slip_created = _slip_created.replace(tzinfo=_ZIpg2("America/New_York"))
                        _since_created = (_now.replace(tzinfo=_ZIpg2("America/New_York")) - _slip_created).total_seconds() / 3600
                        if _since_created <= 4:  # only check fresh slips
                            _stale_start = False
                            for _pk2 in slip.get("picks", []):
                                _ct2 = _pk2.get("commence_time", "")
                                if _ct2:
                                    _gd2 = _dp_pg2(_ct2)
                                    if _gd2.tzinfo is None:
                                        _gd2 = _gd2.replace(tzinfo=_ZIpg2("America/New_York"))
                                    _mins_before_creation = (_slip_created - _gd2).total_seconds() / 60
                                    if _mins_before_creation >= 30:  # game started 30+ min before slip was saved
                                        _stale_start = True
                                        logger.warning("Slip %s: pick game started %.0f min before slip was created — voiding", sid, _mins_before_creation)
                                        break
                            if _stale_start:
                                slip["status"] = "void"
                                r.hset(_SLIP_KEY, sid, json.dumps(slip))
                                _db_save_slip(slip)
                                removed += 1
                                continue
                except Exception:
                    pass
                # Force-settle any slip from a previous date — games from yesterday are over.
                # Before marking dead, try one final result check — Sofascore may have
                # updated overnight even if it wasn't available during the game window.
                if (today - slip_day).days >= 1:
                    _final_results = []
                    try:
                        for _pk_idx_f, _pk_f in enumerate(slip.get("picks", [])):
                            # Check Redis cache first (written same-day when Sofascore had it)
                            _cached_res = r.get(f"picks:result:{sid}:{_pk_idx_f}")
                            if _cached_res:
                                _final_results.append(_cached_res if isinstance(_cached_res, str) else _cached_res.decode())
                            else:
                                _res_f = _check_pick_result(_pk_f)
                                if _res_f:
                                    _final_results.append(_res_f)
                    except Exception:
                        pass
                    if _final_results and len(_final_results) == len(slip.get("picks", [])):
                        _final_outcome = "cashed" if all(x == "won" for x in _final_results) else "dead"
                        slip["status"] = _final_outcome
                        r.hset(_SLIP_KEY, sid, json.dumps(slip))
                        _db_save_slip(slip)
                        _ratio = _update_ratio(r, _final_outcome)
                        _db_save_result(slip, _final_outcome)
                        removed += 1
                        logger.info("Force-settled previous-day slip %s → %s", sid, _final_outcome)
                        # Post result to Discord — user needs to know CASHED/DEAD even if settled next morning
                        _result_key = f"game:result:{sid}"
                        if not _alerted(r, _result_key):
                            try:
                                _alert_result(slip, _final_outcome, _ratio, _final_results)
                                _mark_alerted(r, _result_key)
                            except Exception as _ae:
                                logger.warning("Force-settle Discord alert failed: %s", _ae)
                    else:
                        # Still no result — mark void, not dead (data gap, not a real loss)
                        slip["status"] = "void"
                        r.hset(_SLIP_KEY, sid, json.dumps(slip))
                        _db_save_slip(slip)
                        removed += 1
                        logger.warning("Force-settled previous-day slip %s → void (no result found)", sid)
        except Exception:
            pass
    # Remove all void slips from Redis (log-only, no Discord, no W/L impact)
    for sid, raw in list(r.hgetall(_SLIP_KEY).items()):
        try:
            slip = json.loads(raw)
            if slip.get("status") == "void":
                r.hdel(_SLIP_KEY, sid)
                removed += 1
                logger.info("Purged void slip: %s", sid)
        except Exception:
            pass

    return removed


def _db_path() -> str:
    import os
    return os.getenv("SIP_DB_PATH", "data/sip.db")


def _db_save_slip(slip: dict) -> None:
    """Persist slip to SQLite so it survives Redis/bot restarts."""
    import sqlite3
    try:
        conn = sqlite3.connect(_db_path())
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slip_tracker (
                slip_id   TEXT PRIMARY KEY,
                slip_json TEXT NOT NULL,
                saved_at  TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO slip_tracker (slip_id, slip_json, saved_at) VALUES (?, ?, ?)",
            (slip["id"], json.dumps(slip), _now_et().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("slip_tracker: DB persist failed: %s", e)


def _get_week_start() -> str:
    """Return the ISO date of the most recent Monday (start of current week)."""
    from src.core.timezone import et_naive
    today = et_naive()
    monday = today - timedelta(days=today.weekday())  # weekday() == 0 on Monday
    return monday.strftime("%Y-%m-%d")


def _get_weekly_ratio(r) -> dict:
    """Return this week's W/L. Resets automatically when week rolls over."""
    raw = r.hgetall(_WEEKLY_KEY)
    week_start = _get_week_start()
    if raw.get("week_start") != week_start:
        # New week — reset
        r.hset(_WEEKLY_KEY, mapping={"wins": 0, "losses": 0, "week_start": week_start})
        r.persist(_WEEKLY_KEY)
        return {"wins": 0, "losses": 0}
    return {
        "wins":   int(raw.get("wins",   0) or 0),
        "losses": int(raw.get("losses", 0) or 0),
    }


def _update_weekly_ratio(r, result: str) -> dict:
    week_start = _get_week_start()
    raw = r.hgetall(_WEEKLY_KEY)
    if raw.get("week_start") != week_start:
        r.hset(_WEEKLY_KEY, mapping={"wins": 0, "losses": 0, "week_start": week_start})
    if result in ("cashed", "won"):
        r.hincrby(_WEEKLY_KEY, "wins",   1)
    else:
        r.hincrby(_WEEKLY_KEY, "losses", 1)
    r.persist(_WEEKLY_KEY)
    return _get_weekly_ratio(r)


def _db_init_history(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slip_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slip_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            period      TEXT NOT NULL,
            platform    TEXT NOT NULL,
            pick_names  TEXT NOT NULL,
            result      TEXT NOT NULL,
            settled_at  TEXT NOT NULL
        )
    """)
    conn.commit()


def _db_save_result(slip: dict, result: str) -> None:
    """Record a settled slip in the tracklist. One row per settled slip."""
    import sqlite3
    try:
        picks = slip.get("picks", [])
        names = []
        for p in picks:
            n = (p.get("question") or p.get("player") or
                 f"{p.get('away_team', '')} @ {p.get('home_team', '')}").strip(" @") or "?"
            names.append(n)
        conn = sqlite3.connect(_db_path())
        _db_init_history(conn)
        # Don't duplicate — check slip_id not already in history
        existing = conn.execute(
            "SELECT 1 FROM slip_history WHERE slip_id=?", (slip["id"],)
        ).fetchone()
        if existing:
            conn.close()
            return
        conn.execute(
            """INSERT INTO slip_history
               (slip_id, date, period, platform, pick_names, result, settled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                slip["id"],
                slip["id"].split(":")[-1] if ":" in slip["id"] else _now_et().strftime("%Y-%m-%d"),
                slip.get("period", ""),
                slip.get("platform", ""),
                " | ".join(names),
                result,
                _now_et().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("slip_history: DB write failed: %s", e)


def get_tracklist(limit: int = 50) -> list[dict]:
    """Return the last `limit` settled slips from the history table, newest first."""
    import sqlite3
    try:
        conn = sqlite3.connect(_db_path())
        _db_init_history(conn)
        rows = conn.execute(
            """SELECT slip_id, date, period, platform, pick_names, result, settled_at
               FROM slip_history ORDER BY settled_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {"slip_id": r[0], "date": r[1], "period": r[2], "platform": r[3],
             "pick_names": r[4], "result": r[5], "settled_at": r[6]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("get_tracklist: DB read failed: %s", e)
        return []


def get_weekly_tracklist(week_start: str | None = None) -> dict:
    """
    Return wins + losses for a specific week, plus the full slip list for that week.
    week_start: "YYYY-MM-DD" (Monday). Defaults to the current week.

    Returns:
        {
          "week_start": "2026-07-06",
          "wins": 3, "losses": 1,
          "slips": [ {slip row dicts} ... ]
        }
    """
    import sqlite3
    if not week_start:
        week_start = _get_week_start()
    # week ends 7 days after Monday (exclusive)
    from datetime import date
    try:
        _ws = date.fromisoformat(week_start)
        _we = (_ws + timedelta(days=7)).isoformat()
    except ValueError:
        logger.warning("get_weekly_tracklist: invalid week_start '%s'", week_start)
        return {"week_start": week_start, "wins": 0, "losses": 0, "slips": []}

    try:
        conn = sqlite3.connect(_db_path())
        _db_init_history(conn)
        rows = conn.execute(
            """SELECT slip_id, date, period, platform, pick_names, result, settled_at
               FROM slip_history
               WHERE date >= ? AND date < ?
               ORDER BY settled_at ASC""",
            (week_start, _we),
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("get_weekly_tracklist: DB read failed: %s", e)
        return {"week_start": week_start, "wins": 0, "losses": 0, "slips": []}

    slips = [
        {"slip_id": r[0], "date": r[1], "period": r[2], "platform": r[3],
         "pick_names": r[4], "result": r[5], "settled_at": r[6]}
        for r in rows
    ]
    wins   = sum(1 for s in slips if s["result"] == "cashed")
    losses = sum(1 for s in slips if s["result"] == "dead")
    return {"week_start": week_start, "wins": wins, "losses": losses, "slips": slips}


def _is_flag_set(flag_key: str) -> bool:
    """Check if a one-time flag is set — checks SQLite first (survives Redis restart), then Redis."""
    try:
        import sqlite3
        conn = sqlite3.connect(_db_path())
        conn.execute("CREATE TABLE IF NOT EXISTS bot_flags (flag_key TEXT PRIMARY KEY, set_at TEXT NOT NULL)")
        conn.commit()
        row = conn.execute("SELECT 1 FROM bot_flags WHERE flag_key=?", (flag_key,)).fetchone()
        conn.close()
        if row:
            return True
    except Exception:
        pass
    # Fallback: check Redis
    try:
        r = _redis()
        return bool(r.get(flag_key))
    except Exception:
        return False


def _set_flag(flag_key: str) -> None:
    """Set a one-time flag in both SQLite and Redis so it survives restarts."""
    try:
        import sqlite3
        conn = sqlite3.connect(_db_path())
        conn.execute("CREATE TABLE IF NOT EXISTS bot_flags (flag_key TEXT PRIMARY KEY, set_at TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO bot_flags (flag_key, set_at) VALUES (?, ?)",
                     (flag_key, _now_et().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("_set_flag SQLite failed for %s: %s", flag_key, e)
    try:
        r = _redis()
        r.set(flag_key, "1")
    except Exception:
        pass


def adjust_record(wins_delta: int = 0, losses_delta: int = 0) -> dict:
    """
    Adjust W/L counts by delta (positive or negative).
    Used to correct ghost slip credits without full reset.
    Floors at 0 — can't go below zero.
    """
    r = _redis()
    w = max(0, int(r.hget(_RATIO_KEY, "wins") or 0) + wins_delta)
    l = max(0, int(r.hget(_RATIO_KEY, "losses") or 0) + losses_delta)
    r.hset(_RATIO_KEY, mapping={"wins": w, "losses": l})
    r.persist(_RATIO_KEY)
    ww = max(0, int(r.hget(_WEEKLY_KEY, "wins") or 0) + wins_delta)
    wl = max(0, int(r.hget(_WEEKLY_KEY, "losses") or 0) + losses_delta)
    r.hset(_WEEKLY_KEY, mapping={"wins": ww, "losses": wl})
    r.persist(_WEEKLY_KEY)
    logger.info("Record adjusted by %+dW %+dL → %dW-%dL (all-time), %dW-%dL (weekly)",
                wins_delta, losses_delta, w, l, ww, wl)
    return {"wins": w, "losses": l, "weekly_wins": ww, "weekly_losses": wl}


def reset_record() -> None:
    """
    Reset all W/L tracking to zero.
    - Clears all-time ratio (slips:ratio)
    - Clears weekly ratio (slips:weekly)
    - Leaves slip_history intact (historical rows still there for reference)
    Call this once for a fresh start; safe to call again anytime.
    """
    r = _redis()
    r.hset(_RATIO_KEY,  mapping={"wins": 0, "losses": 0})
    r.hset(_WEEKLY_KEY, mapping={"wins": 0, "losses": 0, "week_start": _get_week_start()})
    r.persist(_RATIO_KEY)
    r.persist(_WEEKLY_KEY)
    logger.info("Record reset to 0W-0L (all-time and weekly)")


def post_record_to_discord() -> None:
    """
    Post the full all-time record + recent tracklist to Discord.
    Call from VPS: python -c "from src.workers.slip_tracker import post_record_to_discord; post_record_to_discord()"
    """
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post

    r = _redis()
    ratio   = _get_ratio(r)
    weekly  = _get_weekly_ratio(r)
    history = get_tracklist(limit=30)

    w, l   = ratio["wins"], ratio["losses"]
    ww, wl = weekly["wins"], weekly["losses"]
    total  = w + l
    win_pct = f"  ·  {round(w / total * 100)}% win rate" if total > 0 else ""

    # Build tracklist lines — last 20, newest first
    lines = []
    for h in history[:20]:
        icon = "✅" if h["result"] == "cashed" else "❌"
        plat = h["platform"].upper()[:2]
        period = h["period"].upper()[0] if h["period"] else "?"
        date = h["date"]
        picks_short = h["pick_names"][:60] + ("…" if len(h["pick_names"]) > 60 else "")
        lines.append(f"{icon} `{date}` [{plat}/{period}]  {picks_short}")

    desc = (
        f"**All-time:**  `{w}W – {l}L{win_pct}`\n"
        f"**This week:** `{ww}W – {wl}L`\n\n"
        + ("**Recent Results:**\n" + "\n".join(lines) if lines else "_No settled slips yet._")
    )

    embed = {
        "title": "📊  Full Record & History",
        "description": desc[:4096],
        "color": 0x1565C0,
        "footer": {"text": f"Showing last {len(history)} settled slips"},
    }
    _run_async(_post({"embeds": [embed]}))
    logger.info("Record posted to Discord: %dW-%dL all-time, %dW-%dL this week", w, l, ww, wl)


def reload_slips_from_db() -> int:
    """
    Called on bot startup — reloads active slips from SQLite into Redis.
    Prevents ghost slips AND ensures tracking survives restarts.
    Only reloads slips from the last 2 days that are still active.
    """
    import sqlite3
    from datetime import datetime, timedelta
    import zoneinfo
    ET     = zoneinfo.ZoneInfo("America/New_York")
    cutoff = (datetime.now(ET) - timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(_db_path())
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slip_tracker (
                slip_id   TEXT PRIMARY KEY,
                slip_json TEXT NOT NULL,
                saved_at  TEXT NOT NULL
            )
        """)
        rows = conn.execute(
            "SELECT slip_id, slip_json FROM slip_tracker WHERE saved_at >= ?",
            (cutoff,)  # time-based filter — covers all platforms for last 2 days
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("reload_slips_from_db: DB read failed: %s", e)
        return 0

    r = _redis()
    loaded = 0
    for slip_id, slip_json in rows:
        try:
            slip = json.loads(slip_json)
            # Only reload active slips — don't resurrect settled ones
            if slip.get("status") != "active":
                continue
            # Don't overwrite if already in Redis (fresh bot restart may still have it)
            if not r.hexists(_SLIP_KEY, slip_id):
                r.hset(_SLIP_KEY, slip_id, slip_json)
                loaded += 1
        except Exception:
            pass
    r.persist(_SLIP_KEY)
    if loaded:
        logger.info("reload_slips_from_db: restored %d active slip(s) into Redis", loaded)
    return loaded


def save_slip(period: str, platform: str, picks: list[dict], ticket_id: str | None = None) -> str:
    """
    Called right after an entry is posted to Discord.
    Saves the slip to Redis AND SQLite so it survives restarts.
    Only ONE slip per period per platform per day is saved — prevents
    duplicate tracking alerts from multiple restarts.

    period:     "day" | "night"
    platform:   "hardrock" | "kalshi"
    picks:      list of pick dicts from the entry generator
    ticket_id:  short display ID shown in the original entry embed (for consistency)
    """
    r = _redis()

    # One slip per period per platform per day — overwrite if exists
    today   = et_naive().strftime("%Y-%m-%d")
    slip_id = f"{period}:{platform}:{today}"

    slip = {
        "id":        slip_id,
        "period":    period,
        "platform":  platform,
        "created":   _now_et().isoformat(),
        "picks":     picks,
        "status":    "active",   # active | cashed | dead
    }
    if ticket_id:
        slip["ticket_id"] = ticket_id
    r.hset(_SLIP_KEY, slip_id, json.dumps(slip))
    r.persist(_SLIP_KEY)  # never auto-expire — cleanup_old_slips removes individual fields; TTL would wipe all slips
    _db_save_slip(slip)   # persist to SQLite — survives Redis flush / bot restart
    logger.info("Slip saved: %s (%d picks)", slip_id, len(picks))
    return slip_id


def _load_active_slips(r) -> list[dict]:
    all_slips = r.hgetall(_SLIP_KEY)
    out = []
    from datetime import timedelta as _td_cut
    from src.core.timezone import et_naive as _et_naive_cut
    cutoff = (_et_naive_cut() - _td_cut(hours=36)).isoformat()
    for sid, raw in all_slips.items():
        try:
            slip = json.loads(raw)
            status = slip.get("status", "active")
            if status == "active":
                out.append(slip)
            elif status == "dead":
                # Re-check Kalshi slips marked dead by timeout — Kalshi may have settled since
                picks = slip.get("picks", [])
                has_kalshi = any(p.get("question") or p.get("market_id") for p in picks)
                created = slip.get("created", "")
                if has_kalshi and created >= cutoff:
                    # Check if Kalshi now has a definitive result (not unknown/timeout)
                    for p in picks:
                        if p.get("question") or p.get("market_id"):
                            real_res = _check_kalshi_result_only(p)
                            if real_res in ("won", "lost"):
                                # Kalshi settled after our timeout — correct the result
                                slip["status"] = "active"
                                slip["_rechecking"] = True
                                out.append(slip)
                                break
        except Exception:
            pass
    return out


def _save_slip(r, slip: dict) -> None:
    r.hset(_SLIP_KEY, slip["id"], json.dumps(slip))
    _db_save_slip(slip)   # keep DB in sync on every status update


def _alerted(r, key: str) -> bool:
    if r.sismember(_ALERTED_KEY, key):
        return True
    # Also check SQLite — survives Redis/bot restarts
    try:
        import sqlite3
        conn = sqlite3.connect(_db_path())
        conn.execute("CREATE TABLE IF NOT EXISTS alerted_keys (key TEXT PRIMARY KEY, saved_at TEXT NOT NULL)")
        conn.commit()
        row = conn.execute("SELECT 1 FROM alerted_keys WHERE key=?", (key,)).fetchone()
        conn.close()
        if row:
            r.sadd(_ALERTED_KEY, key)  # warm Redis cache
            return True
    except Exception:
        pass
    return False


def _mark_alerted(r, key: str) -> None:
    r.sadd(_ALERTED_KEY, key)
    r.expire(_ALERTED_KEY, 86400)
    # Persist to SQLite so alerts don't re-fire after restart
    try:
        import sqlite3
        conn = sqlite3.connect(_db_path())
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerted_keys (
                key      TEXT PRIMARY KEY,
                saved_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO alerted_keys (key, saved_at) VALUES (?, ?)",
            (key, _now_et().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── W/L ratio ─────────────────────────────────────────────────────────────────

def _get_ratio(r) -> dict:
    raw = r.hgetall(_RATIO_KEY)
    return {
        "wins":   int(raw.get("wins",   0)),
        "losses": int(raw.get("losses", 0) or 0) + int(raw.get("pushes", 0) or 0),  # absorb old pushes into losses
    }


def _update_ratio(r, result: str) -> dict:
    if result in ("cashed", "won"):
        r.hincrby(_RATIO_KEY, "wins",   1)
    else:
        r.hincrby(_RATIO_KEY, "losses", 1)  # push = loss — no draws in this system
    r.persist(_RATIO_KEY)  # W/L ratio is permanent running total — never auto-expire
    _update_weekly_ratio(r, result)
    return _get_ratio(r)


# ── Discord posts ──────────────────────────────────────────────────────────────

def _post_embed(embed: dict) -> None:
    from src.discord_bot.bot import _post
    from src.workers.alert_worker import _run_async
    try:
        _run_async(_post({"embeds": [embed]}))
    except Exception as e:
        logger.error("Slip alert post failed: %s", e)


def _platform_label(platform: str) -> str:
    return {"hardrock": "HardRock", "kalshi": "Kalshi"}.get(platform, platform.title())


def _slip_legs(picks: list[dict], results: list[str] | None = None) -> str:
    """Render each leg in slip format. Pass results=['won','lost',...] to show outcome per leg."""
    _MARKET = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}
    _OUTCOME = {"won": "✅  WON", "cashed": "✅  CASHED", "lost": "❌  LOST", "dead": "❌  DEAD"}
    lines = []
    for i, p in enumerate(picks, 1):
        conf    = round((p.get("confidence") or 0) * 100)
        outcome = _OUTCOME.get((results[i - 1] if results and i <= len(results) else ""), "")
        outcome_line = f"\n┗  {outcome}" if outcome else ""

        if p.get("question"):
            # Kalshi/prediction market pick
            side = p.get("side", "yes").upper()
            lines.append(
                f"`LEG {i}`  🔵 **{p['question']}**\n"
                f"┣  Answer **{side}**  ·  Conf **{conf}%**"
                + outcome_line
            )
        elif p.get("type") == "prop":
            tag = "🏟️" if p.get("is_team_prop") else "👤"
            lines.append(
                f"`LEG {i}`  {tag} **{p['player']}**\n"
                f"┣  {p['stat']} **{p['direction']} {p['line']}**\n"
                f"┣  Conf **{conf}%**"
                + outcome_line
            )
        else:
            mkt = _MARKET.get(p.get("market", ""), p.get("market", "").upper())
            fmt_odds = (lambda v: f"+{v}" if isinstance(v, (int, float)) and v > 0 else str(v))(p.get("best_odds", ""))
            _lv = p.get("line_value")
            _lv_str = ""
            if _lv is not None and p.get("market") in ("spreads", "totals"):
                try:
                    _lv_str = f" {'+' if float(_lv) > 0 else ''}{_lv}"
                except Exception:
                    pass
            lines.append(
                f"`LEG {i}`  **{p.get('away_team', '')} @ {p.get('home_team', '')}**\n"
                f"┣  {mkt}  **{p.get('selection', '')}{_lv_str}**  `{fmt_odds}`\n"
                f"┣  Conf **{conf}%**"
                + outcome_line
            )
    return "\n".join(lines) or "—"


def _ticket_header(slip: dict) -> str:
    platform  = _platform_label(slip["platform"])
    period    = slip.get("period", "").upper()
    ticket_id = slip.get("ticket_id") or slip.get("id", "")[-8:].upper()
    n         = len(slip["picks"])
    return (
        f"```\n"
        f"  {platform.upper()} BET SLIP  ·  {period}\n"
        f"  Ticket #{ticket_id}    {n}-LEG\n"
        f"```"
    )



def _alert_result(slip: dict, result: str, ratio: dict, results: list[str] | None = None,
                  weekly: dict | None = None) -> None:
    if weekly is None:
        try:
            weekly = _get_weekly_ratio(_redis())
        except Exception:
            weekly = {"wins": 0, "losses": 0}

    platform = _platform_label(slip["platform"])
    w, l     = ratio["wins"], ratio["losses"]
    ww, wl   = weekly["wins"], weekly["losses"]
    total    = w + l
    pct_str  = f"  ·  {round(w / total * 100)}% win rate" if total > 0 else ""
    alltime  = f"{w}W – {l}L{pct_str}"
    this_wk  = f"{ww}W – {wl}L"

    if result == "cashed":
        title  = "✅  SLIP CASHED"
        stamp  = "W I N N E R"
        color  = 0x1B5E20
        footer = f"🎉 All legs hit · {platform} · This week: {this_wk}  ·  All-time: {alltime}"
    else:
        title  = "❌  SLIP DEAD"
        stamp  = "L O S T"
        color  = 0xB71C1C
        footer = f"💔 A leg missed · {platform} · This week: {this_wk}  ·  All-time: {alltime}"

    _post_embed({
        "title":       title,
        "description": (
            f"{_ticket_header(slip)}\n"
            f"```\n"
            f"  *** {stamp} ***\n"
            f"```\n"
            f"{_slip_legs(slip['picks'], results)}"
        ),
        "color": color,
        "footer": {"text": footer},
    })


# ── Result checking ────────────────────────────────────────────────────────────

def _check_kalshi_result_only(pick: dict) -> str | None:
    """Quick Kalshi API check — only returns won/lost from the API, no fallback."""
    market_id = pick.get("market_id", "")
    if not market_id:
        return None
    try:
        from src.apis.kalshi import _get
        data = _get(f"/markets/{market_id}")
        if not data:
            return None
        m = data.get("market", data)
        result = (m.get("result") or "").lower()
        if result not in ("yes", "no"):
            return None
        answer = (pick.get("answer") or pick.get("side") or "yes").lower()
        if result == "yes":
            return "won" if answer == "yes" else "lost"
        return "won" if answer == "no" else "lost"
    except Exception:
        return None


def _check_kalshi_result(pick: dict) -> str | None:
    """Check Kalshi market result via the API using stored market_id."""
    market_id = pick.get("market_id", "")
    if not market_id:
        # No market_id = came from Odds API fallback, not a real Kalshi market.
        # Fall through to score-based settlement using home/away team names.
        home = (pick.get("home_team") or "").lower()
        away = (pick.get("away_team") or "").lower()
        sport = pick.get("sport_key", "")
        if not (home or away) or not sport:
            logger.warning("Kalshi result check: no market_id and no team info in pick '%s'", pick.get("question", "?"))
            return None
        try:
            from src.engines.odds_engine import fetch_scores
            scores = fetch_scores(sport, days_from=14)
            answer = (pick.get("answer") or pick.get("side") or "yes").lower()
            for item in scores:
                if not item.get("completed"):
                    continue
                ih = (item.get("home_team") or "").lower()
                ia = (item.get("away_team") or "").lower()
                if not ((home and (home in ih or ih in home)) or (away and (away in ia or ia in away))):
                    continue
                score_list = item.get("scores") or []
                if not score_list:
                    continue
                sorted_s = sorted(score_list, key=lambda s: float(s.get("score", 0) or 0), reverse=True)
                if len(sorted_s) < 2 or sorted_s[0].get("score") == sorted_s[1].get("score"):
                    return "lost"  # draw/tie = lost (no push)
                winner = (sorted_s[0].get("name") or "").lower()
                selection = (pick.get("selection") or pick.get("home_team") or home or "").lower()
                selection_won = bool(selection and (selection in winner or winner in selection))
                if answer == "yes":
                    return "won" if selection_won else "lost"
                else:
                    return "won" if not selection_won else "lost"
        except Exception as e:
            logger.warning("Kalshi fallback score check failed: %s", e)
        return None
    try:
        from src.apis.kalshi import _get
        data = _get(f"/markets/{market_id}")
        if not data:
            settled = _get("/markets", {"status": "settled", "limit": 200})
            if settled:
                markets = settled.get("markets", []) if isinstance(settled, dict) else []
                for m in markets:
                    if m.get("ticker", "") == market_id:
                        data = {"market": m}
                        break
        if not data:
            logger.warning("Kalshi result check: no data returned for market_id=%s", market_id)
            return None
        m = data.get("market", data)
        result = (m.get("result") or "").lower()
        status = (m.get("status") or "").lower()
        logger.info("Kalshi market %s: status=%s result=%s", market_id, status, result)
        if not result or result in ("", "unknown", "void"):
            return None
        answer = (pick.get("answer") or pick.get("side") or "yes").lower()
        if result == "yes":
            return "won" if answer == "yes" else "lost"
        elif result == "no":
            return "won" if answer == "no" else "lost"
        return None
    except Exception as e:
        logger.warning("Kalshi result check failed for %s: %s", market_id, e)
        return None


def _check_sofascore_result(pick: dict) -> str | None:
    """
    Try Sofascore for the game result — faster and more reliable than Kalshi/Odds API.
    Returns 'won'/'lost' if Sofascore has a finished result, else None.
    """
    try:
        from src.apis.sofascore import get_event_result, find_event_by_teams
        sf_id   = pick.get("sofascore_id", "")
        home    = (pick.get("home_team") or "").strip()
        away    = (pick.get("away_team") or "").strip()
        answer  = (pick.get("answer") or pick.get("side") or pick.get("selection") or "yes").lower()

        sf_ev = None
        if sf_id:
            sf_ev = get_event_result(sf_id)
        if not sf_ev and home and away:
            # Pass the game date so we search the right day's events (yesterday's games
            # are NOT in today's sofascore:today_events cache)
            _ct = _parse_time(pick.get("commence_time", ""))
            _date_str = _ct.strftime("%Y-%m-%d") if _ct else None
            ev_found = find_event_by_teams(home, away, date_str=_date_str)
            if ev_found:
                _resolved_id = str(ev_found.get("id", ""))
                # Cache resolved sofascore_id back into pick so future polls skip the lookup
                if _resolved_id and not sf_id:
                    pick["sofascore_id"] = _resolved_id
                sf_ev = get_event_result(_resolved_id) if _resolved_id else None

        if not sf_ev or not sf_ev.get("is_finished"):
            return None  # not finished yet — keep polling

        hs = sf_ev.get("home_score")
        as_ = sf_ev.get("away_score")
        sf_home = sf_ev.get("home_team", "").lower()
        sf_away = sf_ev.get("away_team", "").lower()
        winner  = sf_ev.get("winner", "unknown").lower()

        # Determine if the pick's selection won
        sel = (pick.get("selection") or home or "").lower()
        market = pick.get("market", "h2h")

        if market == "totals":
            if hs is None or as_ is None:
                return None
            total = hs + as_
            line  = pick.get("line") or pick.get("line_value")
            direction = (pick.get("direction") or pick.get("selection") or "").lower()
            if not line:
                return None
            try:
                line_f = float(line)
                if abs(total - line_f) < 0.1:
                    return "lost"  # CASHED/DEAD only — exact total = lost
                return "won" if (total > line_f and "over" in direction) or (total < line_f and "under" in direction) else "lost"
            except Exception:
                return None

        if market == "spreads":
            if hs is None or as_ is None:
                return None
            line_val = pick.get("line_value")
            if line_val is None:
                return None
            is_home = bool(sel and (sel in sf_home or sf_home in sel))
            margin  = (hs - as_) if is_home else (as_ - hs)
            covered = margin + float(line_val)
            if abs(covered) < 0.1:
                return "lost"  # CASHED/DEAD only — exact spread = lost
            return "won" if covered > 0 else "lost"

        if winner == "unknown" or (hs is None or as_ is None):
            return None

        # Kalshi YES/NO questions — detect question type from wording
        # Must come BEFORE the draw check — goals/totals props on a 1-1 game should
        # still resolve correctly (over 0.5 goals = won), not return "push".
        if pick.get("question"):
            q = pick.get("question", "").lower()
            answer = (pick.get("answer") or pick.get("side") or "yes").lower()

            # Goals/points/runs scored props — "will over/under X goals/points be scored"
            import re as _re
            _total_match = _re.search(r"(over|under)\s+([\d.]+)", q)
            if _total_match or any(w in q for w in ("goal", "point", "run", "score", "total")):
                if hs is None or as_ is None:
                    return None
                total_scored = hs + as_
                if _total_match:
                    direction = _total_match.group(1)  # "over" or "under"
                    line_f    = float(_total_match.group(2))
                    if abs(total_scored - line_f) < 0.1:
                        return "lost"  # exact = lost (no push)
                    prop_won = (total_scored > line_f) if direction == "over" else (total_scored < line_f)
                else:
                    # "will goals be scored" style — any score > 0 = yes
                    prop_won = total_scored > 0
                return "won" if (answer == "yes" and prop_won) or (answer == "no" and not prop_won) else "lost"

            # Team winner question — "Will X defeat/beat Y?" or "Will X win?"
            # Extract YES team from question text: "Will <team> beat/defeat/win..." → <team>
            yes_team = ""
            _q_match = _re.match(r"will\s+(.+?)\s+(beat|defeat|win|advance|cover)", q)
            if _q_match:
                yes_team = _q_match.group(1).strip()
            if not yes_team:
                # Fallback: check if away_team appears in question, else use home_team
                _ht = (pick.get("home_team") or "").lower()
                _at = (pick.get("away_team") or "").lower()
                if _at and _at in q:
                    yes_team = _at
                else:
                    yes_team = _ht or (pick.get("selection") or "").lower()
            yes_country = (pick.get("home_country") or "").lower()

            def _team_match(name: str, ref: str) -> bool:
                if not name or not ref:
                    return False
                tokens = [t for t in name.split() if len(t) > 3]
                return name in ref or ref in name or any(t in ref or ref in t for t in tokens)

            yes_won = bool(
                _team_match(yes_team, winner) or
                (yes_country and _team_match(yes_country, winner)) or
                (_team_match(yes_team, sf_home) and hs > as_) or
                (_team_match(yes_team, sf_away) and as_ > hs)
            )
            return "won" if (answer == "yes" and yes_won) or (answer == "no" and not yes_won) else "lost"

        # Standard moneyline — draw = lost (no push in this system)
        if winner == "draw":
            return "lost"
        sel_won2 = bool(sel and (
            (sel in sf_home or sf_home in sel) and hs > as_ or
            (sel in sf_away or sf_away in sel) and as_ > hs
        ))
        return "won" if sel_won2 else "lost"
    except Exception as e:
        logger.debug("Sofascore result check failed: %s", e)
        return None


def _check_pick_result(pick: dict) -> str | None:
    """
    Returns 'won', 'lost', or None (not settled yet).
    Sofascore checked first (faster); Kalshi API / Odds API as fallback.
    """
    # Try Sofascore first — it publishes results within minutes of game end
    sf_result = _check_sofascore_result(pick)
    if sf_result is not None:
        return sf_result

    # Kalshi picks resolved via their own API
    if pick.get("question") or pick.get("market_id"):
        return _check_kalshi_result(pick)

    # Rate-limit Odds API score calls — cache result for 5 min per game to avoid burning credits
    _home_key = (pick.get("home_team") or "").lower().replace(" ", "_")
    _away_key = (pick.get("away_team") or "").lower().replace(" ", "_")
    _score_cache_key = f"scores:result:{_home_key}:{_away_key}"
    try:
        _r = _redis()
        _cached_scores = _r.get(_score_cache_key)
        if _cached_scores == "pending":
            return None  # already checked recently — don't burn credits again
    except Exception:
        _r = None

    try:
        from src.engines.odds_engine import fetch_scores
        sport_key = pick.get("sport_key", "")
        if not sport_key:
            return None

        scores = fetch_scores(sport_key, days_from=3)
        # Cache "pending" for 5 min so we don't call Odds API every 30s
        try:
            if _r:
                _r.setex(_score_cache_key, 300, "pending")
        except Exception:
            pass
        home = (pick.get("home_team") or "").lower()
        away = (pick.get("away_team") or "").lower()

        for item in scores:
            if not item.get("completed"):
                continue
            ih = (item.get("home_team") or "").lower()
            ia = (item.get("away_team") or "").lower()
            if not (home in ih or ih in home or away in ia or ia in away):
                continue

            # Matched game — determine result
            score_list = item.get("scores") or []
            if not score_list:
                continue

            try:
                sorted_s = sorted(score_list, key=lambda s: float(s.get("score", 0) or 0), reverse=True)
                winner = sorted_s[0].get("name", "").lower() if len(sorted_s) >= 2 else ""
                selection = (pick.get("selection") or pick.get("player") or "").lower()

                if pick.get("type") == "prop":
                    # Check PropResult DB for settlement written by settlement_worker
                    try:
                        from src.db.session import get_db
                        from src.db.models import PropResult
                        player = (pick.get("player") or "").lower()
                        stat   = (pick.get("stat") or "").lower()
                        with get_db() as db:
                            row = db.query(PropResult).filter(
                                PropResult.subject.ilike(f"%{player}%"),
                                PropResult.stat.ilike(f"%{stat}%"),
                                PropResult.result.isnot(None),
                            ).order_by(PropResult.settled_at.desc()).first()
                            if row:
                                res = row.result
                                return res  # preserve push result
                    except Exception:
                        pass
                    # If game completed but no DB record yet, assume pending
                    return None

                # Moneyline / spread / total
                market = pick.get("market", "h2h")
                if market == "h2h":
                    if not winner or not selection:
                        return None
                    if winner in selection or selection in winner:
                        return "won"
                    elif sorted_s[0].get("score") == sorted_s[1].get("score"):
                        return "lost"  # draw = lost regardless of sport
                    else:
                        return "lost"

                elif market == "spreads":
                    line_val = pick.get("line_value")
                    _ht_raw = item.get("home_team", "")
                    if not _ht_raw:
                        return None  # can't determine spread result without home team
                    if line_val is None:
                        # fallback: treat as moneyline
                        if not winner or not selection:
                            return None
                        return "won" if (winner in selection or selection in winner) else "lost"
                    home_score_val = next((float(s.get("score", 0) or 0) for s in score_list
                                          if _normalize_team_name(s.get("name","")) in
                                          _normalize_team_name(_ht_raw) or
                                          _normalize_team_name(_ht_raw) in
                                          _normalize_team_name(s.get("name",""))), None)
                    _ht_norm = _normalize_team_name(_ht_raw)
                    away_score_val = next((float(s.get("score", 0) or 0) for s in score_list
                                          if _normalize_team_name(s.get("name", "")) != _ht_norm), None)
                    if home_score_val is None or away_score_val is None:
                        return None
                    home_team_norm = _normalize_team_name(item.get("home_team", ""))
                    sel_norm       = _normalize_team_name(selection)
                    is_home = home_team_norm and sel_norm and (
                        home_team_norm in sel_norm or sel_norm in home_team_norm
                    )
                    margin  = (home_score_val - away_score_val) if is_home else (away_score_val - home_score_val)
                    covered = margin + float(line_val)
                    if abs(covered) < 0.1:
                        return "lost"  # CASHED/DEAD only — exact spread = lost
                    return "won" if covered > 0 else "lost"

                elif market == "totals":
                    total = sum(float(s.get("score", 0) or 0) for s in score_list)
                    line = pick.get("line") or pick.get("line_value") or pick.get("total_line")
                    direction = (pick.get("direction") or pick.get("selection") or "").lower()
                    if not line:
                        return None
                    try:
                        line_val = float(line)
                        if total > line_val:
                            return "won" if "over" in direction else "lost"
                        elif total < line_val:
                            return "won" if "under" in direction else "lost"
                        else:
                            return "lost"  # CASHED/DEAD only — exact total = lost
                    except Exception:
                        return None

            except Exception:
                return None

    except Exception as e:
        logger.warning("Result check failed: %s", e)
    return None


# ── Main scan ──────────────────────────────────────────────────────────────────

def _alert_slip_starting_soon(slip: dict, picks: list[dict]) -> None:
    """One embed per slip — shows ticket design with only the legs starting soon."""
    lines = []
    for pick in picks:
        gt   = _fmt_time(pick.get("commence_time", ""))
        name = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
        lines.append(f"**{name}**  ·  🕐 **{gt}**")
    _post_embed({
        "title":       "🔔  GAME STARTING SOON",
        "description": (
            f"{_ticket_header(slip)}\n"
            + "\n".join(lines) +
            f"\n\n{_slip_legs(slip['picks'])}"
        ),
        "color":  0xF9A825,
        "footer": {"text": "⏱️ Get your slip in before tip-off"},
    })


def _alert_slip_live(slip: dict, picks: list[dict]) -> None:
    """One embed per slip — shows ticket design for live games."""
    lines = []
    for pick in picks:
        name = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
        lines.append(f"🔴 **{name}** is LIVE")
    _post_embed({
        "title":       "🔴  GAME NOW LIVE",
        "description": (
            f"{_ticket_header(slip)}\n"
            + "\n".join(lines) +
            f"\n\n{_slip_legs(slip['picks'])}"
        ),
        "color":  0xE53935,
        "footer": {"text": "Tracking result — updates when game ends"},
    })


def track_slips() -> dict:
    """
    Runs every 30 seconds. For each active slip:
    - Fire ONE grouped "starting soon" alert for all games starting within 30 min
    - Fire ONE grouped "live now" alert for all games going live
    - Check results after games complete
    - Mark slip cashed or dead, update W/L ratio
    """
    try:
        r = _redis()
        purge_ghost_slips()
        slips = _load_active_slips(r)
        if not slips:
            return {"slips": 0}

        # Fast-exit: if no game is within 4h of starting, nothing to do this cycle.
        # This keeps the 30s poll cheap when games are hours away.
        now = _now_et()
        _any_active = False
        for _sl in slips:
            for _pk in _sl.get("picks", []):
                _ct = _parse_time(_pk.get("commence_time", ""))
                if _ct and (now - _ct).total_seconds() > -4 * 3600:
                    _any_active = True
                    break
            if _any_active:
                break
        if not _any_active:
            return {"slips": len(slips), "skipped": "no games within 4h"}

        alerts_fired = 0

        # ── Pass 1: starting soon / live alerts via Sofascore status ─────────
        all_soon: list[str] = []
        all_live: list[str] = []

        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")

        def _sf_event(sf_id: str) -> dict:
            """Fetch Sofascore event, cached in Redis for 2 min to avoid repeat calls."""
            try:
                import json as _j
                _cache_key = f"sofascore:event:{sf_id}"
                _cached = r.get(_cache_key)
                if _cached:
                    return _j.loads(_cached)
                from src.apis.sofascore import _get as _sf_get
                data = _sf_get(f"/event/{sf_id}")
                ev = (data or {}).get("event", {}) if data else {}
                if ev:
                    r.setex(_cache_key, 120, _j.dumps(ev))
                return ev
            except Exception:
                return {}

        from datetime import datetime as _dt2

        for slip in slips:
            plat = _platform_label(slip["platform"])
            for pick in slip.get("picks", []):
                sf_id = pick.get("sofascore_id", "")
                # Normalize game ID to home:away so HardRock + Kalshi slips for the
                # same game share one dedup key and never fire duplicate alerts.
                _home = _normalize_team_name(pick.get("home_team", "") or "")
                _away = _normalize_team_name(pick.get("away_team", "") or "")
                gid   = (f"{_home}:{_away}" if _home and _away else None) or \
                        sf_id or pick.get("market_id") or pick.get("event_id") or \
                        (pick.get("question") or pick.get("title") or "unknown")
                name  = (pick.get("subtitle") or pick.get("question") or pick.get("player") or
                         f"{pick.get('away_team','')} @ {pick.get('home_team','')}").strip(" @") or gid
                tag   = f"`[{plat}]`"

                # ── "Starting soon" — use stored commence_time (always available) ──
                # Never skip this alert due to a Sofascore API failure: commence_time
                # is written into the pick dict at entry-generation time and is reliable.
                ct_stored = _parse_time(pick.get("commence_time", ""))
                if ct_stored:
                    mins_to_start = (ct_stored - now).total_seconds() / 60
                    gt = ct_stored.astimezone(_ET).strftime("%-I:%M %p ET")
                    soon_key = f"game:soon:{gid}"
                    if 5 <= mins_to_start <= 60 and not _alerted(r, soon_key):
                        all_soon.append(f"**{name}**  ·  🕐 {gt}  {tag}")
                        _mark_alerted(r, soon_key)

                # ── "Live now" — requires Sofascore status (best-effort) ──────────
                # Resolve sofascore_id: use stored value, else look up by team names
                _resolved_sf_id = sf_id
                if not _resolved_sf_id:
                    _home_t = pick.get("home_team", "")
                    _away_t = pick.get("away_team", "")
                    if _home_t and _away_t:
                        try:
                            from src.apis.sofascore import find_event_by_teams as _fet
                            _ev_match = _fet(_home_t, _away_t)
                            if _ev_match:
                                _resolved_sf_id = str(_ev_match.get("id", ""))
                        except Exception:
                            pass

                sf_status = ""
                start_ts  = None
                if not _resolved_sf_id:
                    logger.warning("slip_tracker: no sofascore_id for pick gid=%s — using time-based live detection", gid)
                else:
                    ev        = _sf_event(_resolved_sf_id)
                    sf_status = (ev.get("status") or {}).get("type", "")
                    start_ts  = ev.get("startTimestamp")

                live_key = f"game:live:{gid}"
                if sf_status == "inprogress" and not _alerted(r, live_key):
                    gt_live = _dt2.fromtimestamp(start_ts, tz=_ET).strftime("%-I:%M %p ET") if start_ts else (
                        ct_stored.astimezone(_ET).strftime("%-I:%M %p ET") if ct_stored else ""
                    )
                    all_live.append(f"🔴 **{name}**" + (f"  ·  🕐 {gt_live}" if gt_live else "") + f"  {tag}")
                    _mark_alerted(r, live_key)

                # Fallback live detection: if no Sofascore status but game started >5 min ago
                # Runs regardless of whether sofascore_id is available — uses stored commence_time
                elif not sf_status and ct_stored:
                    mins_since_start = (now - ct_stored).total_seconds() / 60
                    if 5 <= mins_since_start <= 240 and not _alerted(r, live_key):
                        gt_live = ct_stored.astimezone(_ET).strftime("%-I:%M %p ET")
                        all_live.append(f"🔴 **{name}**  ·  🕐 {gt_live}  {tag}  *(tracking)*")
                        _mark_alerted(r, live_key)

        if all_soon:
            _post_embed({
                "title":       "🔔  GAMES STARTING NOW",
                "description": "\n".join(all_soon),
                "color":       0xF9A825,
                "footer":      {"text": "⏱️ Last chance — tip-off in under 30 min"},
            })
            alerts_fired += 1
        if all_live:
            _post_embed({
                "title":       "🔴  GAMES NOW LIVE",
                "description": "\n".join(all_live),
                "color":       0xE53935,
                "footer":      {"text": "Tracking results — updates when games end"},
            })
            alerts_fired += 1

        # ── Pass 2: settle slips ──────────────────────────────────────────────
        for slip in slips:
            picks = slip.get("picks", [])
            results = []

            for pick in picks:
                ct = _parse_time(pick.get("commence_time", ""))
                _pick_name = (pick.get("subtitle") or pick.get("question") or
                              pick.get("player") or
                              f"{pick.get('away_team','')} @ {pick.get('home_team','')}").strip(" @") or "?"

                # Don't check results before the game has started (10 min buffer)
                if ct and (now - ct).total_seconds() < 10 * 60:
                    _mins_to_start = int((ct - now).total_seconds() / 60)
                    logger.info("Slip %s pick '%s': game starts in %d min — waiting", slip.get("id"), _pick_name, _mins_to_start)
                    continue

                # Try Sofascore first — instant result the moment game ends
                res = _check_pick_result(pick)
                # Persist any sofascore_id that was resolved during the check
                if pick.get("sofascore_id") and not slip.get("_sf_saved"):
                    _save_slip(r, slip)
                    slip["_sf_saved"] = True
                if res:
                    logger.info("Slip %s pick '%s': result=%s", slip.get("id"), _pick_name, res)
                    # Cache so purge_ghost_slips can read it next day (Sofascore drops results)
                    _pick_idx = picks.index(pick)
                    r.setex(f"picks:result:{slip.get('id')}:{_pick_idx}", 172800, res)
                    results.append(res)
                    continue
                logger.info("Slip %s pick '%s': no result yet (ct=%s)", slip.get("id"), _pick_name, pick.get("commence_time", "none"))

                # Not settled yet — check timeout to avoid waiting forever
                if ct and (now - ct).total_seconds() > 24 * 3600:
                    results.append("unknown")
                    logger.warning("Slip %s pick: no result after 24h — marking unknown", slip.get("id"))
                elif not ct:
                    slip_created = _parse_time(slip.get("created", ""))
                    # No commence_time: time out 6h after slip creation (games last at most 4h)
                    if slip_created and (now - slip_created).total_seconds() > 6 * 3600:
                        results.append("unknown")
                        logger.warning("Slip %s: no commence_time and no result after 6h — marking unknown", slip.get("id"))
                else:
                    mins = (ct - now).total_seconds() / 60
                    sport = pick.get("sport_key", "")
                    # Generous timeouts — don't give up before Sofascore posts the result.
                    # Only fire after game has been over long enough that a result must exist.
                    if any(k in sport for k in ("mma", "boxing")):
                        _timeout = -180   # 3h after start
                    elif any(k in sport for k in ("baseball", "mlb")):
                        _timeout = -360   # 6h after start (extra innings, delays)
                    elif any(k in sport for k in ("basketball", "nba", "wnba")):
                        _timeout = -210   # 2.5h game + 1h buffer — Sofascore posts result instantly
                    elif any(k in sport for k in ("tennis",)):
                        _timeout = -300   # 5h after start
                    else:
                        _timeout = -300   # 5h default
                    if mins < _timeout:
                        results.append("unknown")
                        logger.info(
                            "Slip %s: no score after %dh for %s (%s) — marking unknown",
                            slip.get("id"), abs(_timeout) // 60,
                            pick.get("selection") or pick.get("player"), sport,
                        )

            # Settle only when ALL legs have a result (real or timeout-unknown).
            #   all won                → cashed
            #   any confirmed lost     → dead
            #   any unknown (timeout)  → void  (data source failed — don't penalise W/L)
            if results and len(results) == len(picks):
                if all(r == "won" for r in results):
                    slip_result = "cashed"
                elif any(r == "unknown" for r in results):
                    slip_result = "void"   # couldn't verify score — not a real loss
                else:
                    slip_result = "dead"

                result_key = f"game:result:{slip['id']}"
                is_recheck = slip.pop("_rechecking", False)
                if is_recheck and slip_result == "cashed":
                    # Kalshi settled correctly after our timeout — clear old dead alert
                    # and remove the erroneous loss that was already counted
                    r.srem(_ALERTED_KEY, result_key)
                    adjust_record(losses_delta=-1)  # cancel the earlier timeout dead count
                if not _alerted(r, result_key):
                    if slip_result != "void":
                        ratio  = _update_ratio(r, slip_result)
                        weekly = _get_weekly_ratio(r)
                    else:
                        ratio  = _get_ratio(r)
                        weekly = _get_weekly_ratio(r)
                    _db_save_result(slip, slip_result)
                    if slip_result == "void":
                        logger.info("Slip %s voided (score not found after timeout) — not counted in W/L", slip.get("id"))
                        _post_embed({
                            "title": "⚠️  RESULT UNKNOWN",
                            "description": (
                                f"**{_platform_label(slip['platform'])}** · {slip.get('period','').upper()} slip\n"
                                "Score data wasn't available in time — this slip is not counted in W/L.\n"
                                + _slip_legs(picks)
                            ),
                            "color": 0x9E9E9E,
                            "footer": {"text": "Check results manually"},
                        })
                    else:
                        _alert_result(slip, slip_result, ratio, results, weekly=weekly)
                    _mark_alerted(r, result_key)
                    alerts_fired += 1

                slip["status"] = slip_result
                _save_slip(r, slip)

        logger.info("Slip tracker: %d active slips, %d alerts fired", len(slips), alerts_fired)
        return {"slips": len(slips), "alerts": alerts_fired}

    except Exception as exc:
        logger.error("Slip tracker failed: %s", exc)
        return {"error": str(exc)}
