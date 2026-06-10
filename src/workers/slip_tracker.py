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

logger = logging.getLogger(__name__)

_SLIP_KEY    = "slips:active"       # Redis hash: slip_id → slip JSON
_RATIO_KEY   = "slips:ratio"        # Redis hash: wins, losses, pushes
_ALERTED_KEY = "slips:alerted"      # Redis set: {slip_id}:{event} already fired


def _redis():
    from src.core.config import REDIS_URL
    import redis as _r
    return _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(ct: str) -> datetime | None:
    if not ct:
        return None
    try:
        from dateutil.parser import parse as _p
        dt = _p(ct)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
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

def save_slip(period: str, platform: str, picks: list[dict]) -> str:
    """
    Called right after an entry is posted to Discord.
    Saves the slip to Redis so we can track it through game day.

    period:   "day" | "night"
    platform: "hardrock" | "kalshi" | "polymarket"
    picks:    list of pick dicts from the entry generator
    """
    import time
    r = _redis()
    slip_id = f"{period}:{platform}:{int(time.time())}"

    slip = {
        "id":       slip_id,
        "period":   period,
        "platform": platform,
        "created":  _now_utc().isoformat(),
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
    elif result == "dead":
        r.hincrby(_RATIO_KEY, "losses", 1)
    else:
        r.hincrby(_RATIO_KEY, "pushes", 1)
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
    return {"hardrock": "HardRock", "kalshi": "Kalshi", "polymarket": "Polymarket"}.get(platform, platform.title())


def _pick_summary(picks: list[dict]) -> str:
    lines = []
    for p in picks:
        if p.get("type") == "prop":
            lines.append(f"• {p['player']} {p['stat']} {p['direction']} {p['line']}")
        else:
            mkt = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}.get(p.get("market", ""), "")
            lines.append(f"• {p.get('selection', '')} {mkt} {p.get('best_odds', '')}")
    return "\n".join(lines) or "—"


def _conf_bar(picks: list[dict]) -> str:
    avg = sum(p.get("confidence", 0) for p in picks) / max(len(picks), 1)
    filled = round(avg * 10)
    return "🟢" * filled + "⚫" * (10 - filled) + f"  {round(avg * 100)}%"


def _alert_starting_soon(slip: dict, pick: dict) -> None:
    gt       = _fmt_time(pick.get("commence_time", ""))
    platform = _platform_label(slip["platform"])
    name     = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
    period   = slip.get("period", "").capitalize()
    n        = len(slip["picks"])
    _post_embed({
        "title":       f"🔔  Game Starting Soon",
        "description": (
            f"**{name}**\n"
            f"─────────────────────────\n"
            f"🕐  Tip-off in ~30 min  ·  **{gt}**\n"
            f"📋  {platform} {period} Entry  ·  {n} pick{'s' if n != 1 else ''}\n\n"
            f"{_pick_summary(slip['picks'])}\n\n"
            f"{_conf_bar(slip['picks'])}"
        ),
        "color": 0xF9A825,
        "footer": {"text": "Get your entry in now before tip-off ⏱️"},
    })


def _alert_live(slip: dict, pick: dict) -> None:
    platform = _platform_label(slip["platform"])
    name     = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
    period   = slip.get("period", "").capitalize()
    n        = len(slip["picks"])
    _post_embed({
        "title":       f"🔴  LIVE NOW — {name}",
        "description": (
            f"─────────────────────────\n"
            f"📋  {platform} {period} Entry  ·  {n} pick{'s' if n != 1 else ''} on the line\n\n"
            f"{_pick_summary(slip['picks'])}\n\n"
            f"{_conf_bar(slip['picks'])}"
        ),
        "color": 0xE53935,
        "footer": {"text": "Game is live — tracking result 🎯"},
    })


def _alert_result(slip: dict, result: str, ratio: dict) -> None:
    platform    = _platform_label(slip["platform"])
    picks_count = len(slip["picks"])
    period      = slip.get("period", "").capitalize()
    w, l, p     = ratio["wins"], ratio["losses"], ratio.get("pushes", 0)
    total       = w + l

    if result == "cashed":
        icon  = "✅"
        title = f"✅  CASHED — {platform} {period} Entry WON"
        color = 0x1B5E20
        verdict = f"All **{picks_count}** pick{'s' if picks_count != 1 else ''} hit  🎉  Ticket is a winner."
    elif result == "dead":
        icon  = "❌"
        title = f"❌  DEAD — {platform} {period} Entry LOST"
        color = 0xB71C1C
        verdict = f"A pick missed  💔  Ticket is dead."
    else:
        icon  = "➖"
        title = f"➖  PUSH — {platform} {period} Entry"
        color = 0x607D8B
        verdict = "Entry pushed — no win, no loss."

    pct_str = f"  ·  **{round(w / total * 100)}% win rate**" if total > 0 else ""
    record  = f"**{w}W – {l}L**{' – ' + str(p) + 'P' if p else ''}{pct_str}"

    _post_embed({
        "title":       title,
        "description": (
            f"{verdict}\n"
            f"─────────────────────────\n"
            f"{_pick_summary(slip['picks'])}\n"
            f"─────────────────────────\n"
            f"📊  Record:  {record}"
        ),
        "color": color,
        "footer": {"text": f"{platform} · Slip tracking · {slip.get('id', '')}"},
    })


# ── Result checking ────────────────────────────────────────────────────────────

def _check_pick_result(pick: dict) -> str | None:
    """
    Returns 'won', 'lost', 'push', or None (not settled yet).
    Uses Odds API scores endpoint.
    """
    try:
        from src.engines.odds_engine import fetch_scores
        sport_key = pick.get("sport_key", "")
        if not sport_key:
            return None

        scores = fetch_scores(sport_key, days_from=1)
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
                    # Props: check over/under against score
                    line = pick.get("line")
                    direction = (pick.get("direction") or "").lower()
                    stat = (pick.get("stat") or "").lower()
                    # For props we can't fully verify without play-by-play
                    # Mark as pending — settlement_worker handles full prop settlement
                    return None

                # Moneyline / spread / total
                market = pick.get("market", "h2h")
                if market == "h2h":
                    if not winner or not selection:
                        return None
                    if winner in selection or selection in winner:
                        return "won"
                    elif sorted_s[0]["score"] == sorted_s[1]["score"]:
                        return "push"
                    else:
                        return "lost"

                elif market == "totals":
                    total = sum(float(s.get("score", 0) or 0) for s in score_list)
                    line = pick.get("line") or pick.get("best_odds")
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

def track_slips() -> dict:
    """
    Runs every 3 minutes. For each active slip:
    - Fire "starting soon" alert 30 min before kick-off
    - Fire "live now" alert at kick-off
    - Check results after games complete
    - Mark slip cashed or dead, update W/L ratio
    """
    try:
        r = _redis()
        slips = _load_active_slips(r)
        if not slips:
            return {"slips": 0}

        now = _now_utc()
        alerts_fired = 0

        for slip in slips:
            slip_id   = slip["id"]
            picks     = slip.get("picks", [])
            results   = []

            for pick in picks:
                ct = _parse_time(pick.get("commence_time", ""))
                if not ct:
                    continue

                mins_to_game = (ct - now).total_seconds() / 60

                # ── Starting soon (25-35 min window) ──────────────────────
                soon_key = f"{slip_id}:soon:{pick.get('event_id') or pick.get('game_key','')}"
                if 25 <= mins_to_game <= 35 and not _alerted(r, soon_key):
                    _alert_starting_soon(slip, pick)
                    _mark_alerted(r, soon_key)
                    alerts_fired += 1

                # ── Live now (0-5 min past kick-off) ─────────────────────
                live_key = f"{slip_id}:live:{pick.get('event_id') or pick.get('game_key','')}"
                if -5 <= mins_to_game <= 2 and not _alerted(r, live_key):
                    _alert_live(slip, pick)
                    _mark_alerted(r, live_key)
                    alerts_fired += 1

                # ── Result check (game should be done) ───────────────────
                if mins_to_game < -90:   # game started 90+ min ago
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)

            # ── Settle slip if all picks have results ─────────────────────
            if results and len(results) == len([
                p for p in picks
                if _parse_time(p.get("commence_time","")) and
                   (_now_utc() - _parse_time(p.get("commence_time",""))).total_seconds() > 90 * 60
            ]):
                if "lost" in results:
                    slip_result = "dead"
                elif all(r == "won" for r in results):
                    slip_result = "cashed"
                else:
                    slip_result = "push"

                result_key = f"{slip_id}:result"
                if not _alerted(r, result_key):
                    ratio = _update_ratio(r, slip_result)
                    _alert_result(slip, slip_result, ratio)
                    _mark_alerted(r, result_key)
                    alerts_fired += 1

                slip["status"] = slip_result
                _save_slip(r, slip)

        logger.info("Slip tracker: %d active slips, %d alerts fired", len(slips), alerts_fired)
        return {"slips": len(slips), "alerts": alerts_fired}

    except Exception as exc:
        logger.error("Slip tracker failed: %s", exc)
        return {"error": str(exc)}
