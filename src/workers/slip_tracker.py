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
from datetime import datetime, timezone, timedelta
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
    Also removes any slip older than 2 days.
    Returns number of slips removed.
    """
    import re
    from src.core.timezone import et_naive
    r = _redis()
    all_slips = r.hgetall(_SLIP_KEY)
    today     = et_naive().strftime("%Y-%m-%d")
    removed   = 0
    for sid, raw in all_slips.items():
        # Valid IDs look like "day:hardrock:2026-06-13"
        if not re.match(r"^(day|night):[a-z]+:\d{4}-\d{2}-\d{2}$", sid):
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged ghost slip: %s", sid)
        elif sid.split(":")[-1] != today:
            # Keep yesterday's slip if still active — late-ending games (past midnight ET)
            # need one more settlement pass before being purged
            try:
                slip_data = json.loads(raw)
                if slip_data.get("status") == "active":
                    logger.info("Keeping yesterday's active slip for late settlement: %s", sid)
                    continue
            except Exception:
                pass
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged yesterday's slip: %s", sid)
    return removed


def save_slip(period: str, platform: str, picks: list[dict]) -> str:
    """
    Called right after an entry is posted to Discord.
    Saves the slip to Redis so we can track it through game day.
    Only ONE slip per period per platform per day is saved — prevents
    duplicate tracking alerts from multiple restarts.

    period:   "day" | "night"
    platform: "hardrock" | "kalshi"
    picks:    list of pick dicts from the entry generator
    """
    import time
    from src.core.timezone import et_naive
    r = _redis()

    # One slip per period per platform per day — overwrite if exists
    today   = et_naive().strftime("%Y-%m-%d")
    slip_id = f"{period}:{platform}:{today}"

    slip = {
        "id":       slip_id,
        "period":   period,
        "platform": platform,
        "created":  _now_et().isoformat(),
        "picks":    picks,
        "status":   "active",   # active | cashed | dead
    }
    r.hset(_SLIP_KEY, slip_id, json.dumps(slip))
    r.expire(_SLIP_KEY, 86400 * 2)   # 48h TTL
    logger.info("Slip saved: %s (%d picks)", slip_id, len(picks))
    return slip_id


def _load_active_slips(r) -> list[dict]:
    all_slips = r.hgetall(_SLIP_KEY)
    out = []
    for sid, raw in all_slips.items():
        try:
            slip = json.loads(raw)
            if slip.get("status") == "active":
                out.append(slip)
        except Exception:
            pass
    return out


def _save_slip(r, slip: dict) -> None:
    r.hset(_SLIP_KEY, slip["id"], json.dumps(slip))


def _alerted(r, key: str) -> bool:
    return bool(r.sismember(_ALERTED_KEY, key))


def _mark_alerted(r, key: str) -> None:
    r.sadd(_ALERTED_KEY, key)
    r.expire(_ALERTED_KEY, 86400)


# ── W/L ratio ─────────────────────────────────────────────────────────────────

def _get_ratio(r) -> dict:
    raw = r.hgetall(_RATIO_KEY)
    return {
        "wins":   int(raw.get("wins",   0)),
        "losses": int(raw.get("losses", 0)),
        "pushes": int(raw.get("pushes", 0)),
    }


def _update_ratio(r, result: str) -> dict:
    if result == "cashed":
        r.hincrby(_RATIO_KEY, "wins",   1)
    else:
        r.hincrby(_RATIO_KEY, "losses", 1)
    r.persist(_RATIO_KEY)
    return _get_ratio(r)


# ── Discord posts ──────────────────────────────────────────────────────────────

def _post_embed(embed: dict) -> None:
    import asyncio
    from src.discord_bot.bot import _post
    try:
        asyncio.run(_post({"embeds": [embed]}))
    except Exception as e:
        logger.error("Slip alert post failed: %s", e)


def _platform_label(platform: str) -> str:
    return {"hardrock": "HardRock", "kalshi": "Kalshi"}.get(platform, platform.title())


def _slip_legs(picks: list[dict], results: list[str] | None = None) -> str:
    """Render each leg in slip format. Pass results=['won','lost',...] to show outcome per leg."""
    _MARKET = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}
    _OUTCOME = {"won": "✅  WON", "lost": "❌  LOST", "push": "➖  PUSH"}
    lines = []
    for i, p in enumerate(picks, 1):
        conf    = round(p.get("confidence", 0) * 100)
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
    platform = _platform_label(slip["platform"])
    period   = slip.get("period", "").upper()
    slip_id  = slip.get("id", "")[-8:].upper()
    n        = len(slip["picks"])
    return (
        f"```\n"
        f"  {platform.upper()} BET SLIP  ·  {period}\n"
        f"  Ticket #{slip_id}    {n}-LEG\n"
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
            f"{_slip_legs(slip['picks'], results)}\n\n"
            f"📊  **Record:**  {record}"
        ),
        "color": color,
        "footer": {"text": footer},
    })


# ── Result checking ────────────────────────────────────────────────────────────

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
            scores = fetch_scores(sport, days_from=3)
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
                if len(sorted_s) < 2 or sorted_s[0]["score"] == sorted_s[1]["score"]:
                    return "push"
                winner = (sorted_s[0].get("name") or "").lower()
                if answer == "yes":
                    return "won" if (home and (home in winner or winner in home)) else "lost"
                else:
                    return "won" if not (home and (home in winner or winner in home)) else "lost"
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


def _check_pick_result(pick: dict) -> str | None:
    """
    Returns 'won', 'lost', 'push', or None (not settled yet).
    Kalshi picks resolved via Kalshi API; others via Odds API scores.
    """
    # Kalshi picks resolved via their own API
    if pick.get("question") or pick.get("market_id"):
        return _check_kalshi_result(pick)

    try:
        from src.engines.odds_engine import fetch_scores
        sport_key = pick.get("sport_key", "")
        if not sport_key:
            return None

        scores = fetch_scores(sport_key, days_from=3)
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
                                return row.result  # "won" | "lost" | "push"
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
                    elif sorted_s[0]["score"] == sorted_s[1]["score"]:
                        # Draw: in soccer a draw on a team ML pick = lost
                        # Only true push is if the book explicitly offers draw markets
                        sport = pick.get("sport_key", "")
                        if "soccer" in sport or "football" in sport:
                            return "lost"
                        return "push"
                    else:
                        return "lost"

                elif market == "spreads":
                    line_val = pick.get("line_value")
                    if line_val is None:
                        # fallback: treat as moneyline
                        if not winner or not selection:
                            return None
                        return "won" if (winner in selection or selection in winner) else "lost"
                    home_score_val = next((float(s.get("score", 0) or 0) for s in score_list
                                          if _normalize_team_name(s.get("name","")) in
                                          _normalize_team_name(item.get("home_team","")) or
                                          _normalize_team_name(item.get("home_team","")) in
                                          _normalize_team_name(s.get("name",""))), None)
                    away_score_val = next((float(s.get("score", 0) or 0) for s in score_list
                                          if s.get("name") != item.get("home_team","")), None)
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
                        return "push"
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
                            return "push"
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

        # ── Pass 1: collect soon/live — one grouped embed each ────────────────
        all_soon: list[str] = []
        all_live: list[str] = []

        for slip in slips:
            plat   = _platform_label(slip["platform"])
            for pick in slip.get("picks", []):
                ct = _parse_time(pick.get("commence_time", ""))
                if not ct:
                    continue
                mins = (ct - now).total_seconds() / 60
                _home = pick.get("home_team", "")
                _away = pick.get("away_team", "")
                gid  = (pick.get("event_id") or pick.get("game_key") or
                        pick.get("market_id") or
                        (f"{_home}:{_away}" if _home or _away else
                         (pick.get("question") or pick.get("title") or "unknown")))
                _game_name = f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
                name = (pick.get("question") or pick.get("title") or pick.get("player") or
                        (_game_name if _game_name.strip(" @") else ""))
                gt   = _fmt_time(pick.get("commence_time", ""))
                tag  = f"`[{plat}]`"

                soon_key = f"game:soon:{gid}"
                if 0 <= mins <= 30 and not _alerted(r, soon_key):
                    all_soon.append(f"**{name}**  ·  🕐 {gt}  {tag}")
                    _mark_alerted(r, soon_key)

                live_key = f"game:live:{gid}"
                if -180 <= mins <= 5 and not _alerted(r, live_key):
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
                    # Kalshi: check result immediately once close_time has passed
                    if ct and now < ct:
                        continue  # market still open
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)
                    elif not ct or (now - ct).total_seconds() > 14400:
                        # No parseable time OR 4+ hours past close_time with no result —
                        # mark unknown so the slip can resolve rather than deadlocking.
                        results.append("unknown")
                        logger.info(
                            "Slip %s: Kalshi market %s unsettled%s — marking unknown",
                            slip.get("id"), pick.get("market_id", "?"),
                            " (no close_time)" if not ct else " after 4h",
                        )
                else:
                    if not ct:
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
            # unknown = can't verify score — treat as lost (conservative, no push).
            #   all won (no unknowns) → cashed
            #   anything else         → dead
            if results and len(results) == len(picks):
                if not results or any(r != "won" for r in results):
                    slip_result = "dead"
                else:
                    slip_result = "cashed"

                _period_date = f"{slip.get('period','night')}:{slip.get('platform','hardrock')}:{slip.get('created','')[:10]}"
                result_key = f"game:result:{_period_date}"
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
