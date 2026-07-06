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

import re as _re
_SUFFIXES = _re.compile(
    r'\b(fc|city|united|sc|cf|afc|bfc|sporting|athletics)\b', _re.IGNORECASE
)

def _normalize_team_name(name: str) -> str:
    if not name:
        return ""
    name = _SUFFIXES.sub("", name).strip()
    return _re.sub(r'\s+', ' ', name).lower().strip()
_RATIO_KEY   = "slips:ratio"        # Redis hash: wins, losses, pushes
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
        dt = _p(ct)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ZI("America/New_York"))  # naive = ET
        return dt
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
        elif sid.split(":")[-1] not in _keep_dates:
            # 8+ days old — safe to purge
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged old slip: %s", sid)
        # else: within 7-day window — keep for daily/weekly summaries
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
    from datetime import datetime, timedelta
    all_slips = r.hgetall(_SLIP_KEY)
    out = []
    cutoff = (datetime.now() - timedelta(hours=36)).isoformat()
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
        "losses": int(raw.get("losses", 0)),
        "pushes": int(raw.get("pushes", 0)),
    }


def _update_ratio(r, result: str) -> dict:
    if result in ("cashed", "won"):
        r.hincrby(_RATIO_KEY, "wins",   1)
    elif result == "push":
        r.hincrby(_RATIO_KEY, "pushes", 1)
    else:
        r.hincrby(_RATIO_KEY, "losses", 1)
    r.persist(_RATIO_KEY)  # W/L ratio is permanent running total — never auto-expire
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
    _OUTCOME = {"won": "✅  WON", "cashed": "✅  CASHED", "lost": "❌  LOST", "dead": "❌  DEAD", "push": "➖  PUSH"}
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
            lines.append(
                f"`LEG {i}`  **{p.get('away_team', '')} @ {p.get('home_team', '')}**\n"
                f"┣  {mkt}  **{p.get('selection', '')}**  `{fmt_odds}`\n"
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



def _alert_result(slip: dict, result: str, ratio: dict, results: list[str] | None = None) -> None:
    platform = _platform_label(slip["platform"])
    w, l     = ratio["wins"], ratio["losses"]
    total    = w + l
    pct_str  = f"  ·  {round(w / total * 100)}% win rate" if total > 0 else ""
    record   = f"{w}W – {l}L{pct_str}"

    if result == "cashed":
        title  = "✅  SLIP CASHED"
        stamp  = "W I N N E R"
        color  = 0x1B5E20
        footer = f"🎉 All legs hit · {platform} · Record: {record}"
    else:
        title  = "❌  SLIP DEAD"
        stamp  = "L O S T"
        color  = 0xB71C1C
        footer = f"💔 A leg missed · {platform} · Record: {record}"

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
    Returns 'won'/'lost'/'push' if Sofascore has a finished result, else None.
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
            ev_found = find_event_by_teams(home, away)
            if ev_found:
                sf_ev = get_event_result(ev_found.get("id", "")) if ev_found.get("id") else None

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

        # Moneyline / Kalshi question — check if selected team/country won
        if winner == "draw":
            return "push"
        if winner == "unknown" or (hs is None or as_ is None):
            return None

        # For Kalshi binary YES/NO questions, determine if the selected side won
        if pick.get("question"):
            # "Will X defeat Y?" — YES means X wins
            # Use home_team or selection as the "yes" team
            yes_team = (pick.get("home_team") or pick.get("selection") or "").lower()
            yes_country = (pick.get("home_country") or "").lower()
            yes_won = bool(
                (yes_team and (yes_team in winner or winner in yes_team)) or
                (yes_country and (yes_country in winner or winner in yes_country)) or
                (yes_team and (yes_team in sf_home or sf_home in yes_team) and (hs > as_)) or
                (yes_team and (yes_team in sf_away or sf_away in yes_team) and (as_ > hs))
            )
            if answer == "yes":
                return "won" if yes_won else "lost"
            else:
                return "won" if not yes_won else "lost"

        # Standard moneyline
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

    try:
        from src.engines.odds_engine import fetch_scores
        sport_key = pick.get("sport_key", "")
        if not sport_key:
            return None

        scores = fetch_scores(sport_key, days_from=14)
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
    Runs every 3 minutes. For each active slip:
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

        now = _now_et()
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

                if sf_id:
                    ev          = _sf_event(sf_id)
                    sf_status   = (ev.get("status") or {}).get("type", "")
                    start_ts    = ev.get("startTimestamp")
                    if start_ts:
                        from datetime import datetime as _dt2
                        start_et  = _dt2.fromtimestamp(start_ts, tz=_ET)
                        mins      = (start_et - now).total_seconds() / 60
                        gt        = start_et.strftime("%-I:%M %p ET")

                        # Soon: 5–60 min before kickoff
                        soon_key = f"game:soon:{gid}"
                        if 5 <= mins <= 60 and not _alerted(r, soon_key):
                            all_soon.append(f"**{name}**  ·  🕐 {gt}  {tag}")
                            _mark_alerted(r, soon_key)

                    # Live: Sofascore inprogress OR time-based fallback (within 90 min of kickoff)
                    live_key = f"game:live:{gid}"
                    if not _alerted(r, live_key):
                        if sf_status == "inprogress":
                            all_live.append(f"🔴 **{name}**  {tag}")
                            _mark_alerted(r, live_key)
                        elif start_ts:
                            mins_since = (now - _dt2.fromtimestamp(start_ts, tz=_ET)).total_seconds() / 60
                            if 0 < mins_since <= 120:  # strictly after kickoff, up to 2h
                                all_live.append(f"🔴 **{name}**  {tag}")
                                _mark_alerted(r, live_key)
                else:
                    # No sofascore_id — use commence_time
                    ct = _parse_time(pick.get("commence_time", ""))
                    if not ct:
                        logger.warning("slip_tracker: pick has no commence_time — gid=%s name=%s", gid, name)
                        continue
                    mins = (ct - now).total_seconds() / 60
                    gt   = _fmt_time(pick.get("commence_time", ""))

                    # Soon: 5–60 min before kickoff (wide window so tracker never misses)
                    soon_key = f"game:soon:{gid}"
                    if 5 <= mins <= 60 and not _alerted(r, soon_key):
                        all_soon.append(f"**{name}**  ·  🕐 {gt}  {tag}")
                        _mark_alerted(r, soon_key)

                    # Live: strictly after kickoff, within 120 min
                    live_key = f"game:live:{gid}"
                    if -120 <= mins < 0 and not _alerted(r, live_key):
                        all_live.append(f"🔴 **{name}**  {tag}")
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
                is_kalshi = bool(pick.get("question") or pick.get("market_id"))
                if is_kalshi:
                    # Don't check before game has started (or at least 10 min in)
                    if ct and (now - ct).total_seconds() < 10 * 60:
                        continue
                    # Poll Kalshi API directly — it settles within minutes of game end.
                    # No Sofascore gate needed: Kalshi's own result field is the truth.
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)
                    elif ct and (now - ct).total_seconds() > 24 * 3600:
                        # 24h timeout — Kalshi can take hours to settle; mark unknown not dead
                        results.append("unknown")
                        logger.warning("Kalshi slip %s: no settlement after 24h — marking unknown", slip.get("id"))
                    elif not ct:
                        # No commence_time — use slip creation time as 24h fallback
                        slip_created = _parse_time(slip.get("created", ""))
                        if slip_created and (now - slip_created).total_seconds() > 24 * 3600:
                            results.append("unknown")
                            logger.warning("Kalshi slip %s: no settlement after 24h (no commence_time) — marking unknown", slip.get("id"))
                    # Not settled yet — retry next tick
                else:
                    if not ct:
                        # No commence_time: use slip creation time as fallback
                        slip_created = _parse_time(slip.get("created", ""))
                        if slip_created and (now - slip_created).total_seconds() > 24 * 3600:
                            results.append("unknown")
                        continue
                    mins = (ct - now).total_seconds() / 60
                    # Don't check games that haven't started yet or just kicked off
                    if mins > -10:
                        continue
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)
                    else:
                        # Sport-specific timeout — mark unknown once game is certainly over.
                        # Fire result as soon as last game ends, not hours later.
                        sport = pick.get("sport_key", "")
                        if any(k in sport for k in ("mma", "boxing")):
                            _timeout = -120   # 2h
                        elif any(k in sport for k in ("wnba", "nba", "basketball")):
                            _timeout = -180   # 3h — WNBA/NBA games ~2h, no Odds API scores
                        elif any(k in sport for k in ("tennis",)):
                            _timeout = -180   # 3h — tennis matches vary
                        elif any(k in sport for k in ("baseball", "mlb")):
                            _timeout = -240   # 4h — MLB games ~3h
                        else:
                            _timeout = -210   # 3.5h default
                        if mins < _timeout:
                            results.append("unknown")
                            logger.info(
                                "Slip %s: no score after %dh for %s (%s) — marking unknown",
                                slip.get("id"), abs(_timeout) // 60,
                                pick.get("selection") or pick.get("player"), sport,
                            )

            # Settle only when ALL legs have a result (real or timeout-unknown).
            #   all won (no unknowns) → cashed
            #   any unknown           → dead (with note)
            #   any lost              → dead
            if results and len(results) == len(picks):
                if all(r == "won" for r in results):
                    slip_result = "cashed"
                elif any(r == "unknown" for r in results):
                    slip_result = "dead"  # couldn't verify — conservative
                else:
                    slip_result = "dead"

                result_key = f"game:result:{slip['id']}"
                is_recheck = slip.pop("_rechecking", False)
                if is_recheck and slip_result in ("cashed",):
                    # Kalshi settled correctly after our timeout — clear old alert so correct result fires
                    r.srem(_ALERTED_KEY, result_key)
                if not _alerted(r, result_key):
                    ratio = _update_ratio(r, slip_result)
                    _alert_result(slip, slip_result, ratio, results)
                    _mark_alerted(r, result_key)
                    alerts_fired += 1

                slip["status"] = slip_result
                _save_slip(r, slip)

        logger.info("Slip tracker: %d active slips, %d alerts fired", len(slips), alerts_fired)
        return {"slips": len(slips), "alerts": alerts_fired}

    except Exception as exc:
        logger.error("Slip tracker failed: %s", exc)
        return {"error": str(exc)}
