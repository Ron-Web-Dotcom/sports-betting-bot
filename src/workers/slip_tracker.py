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
    Only ONE slip per period per platform per day is saved — prevents
    duplicate tracking alerts from multiple restarts.

    period:   "day" | "night"
    platform: "hardrock" | "kalshi" | "polymarket"
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


def _slip_legs(picks: list[dict]) -> str:
    """Render each leg in slip format."""
    _MARKET = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}
    lines = []
    for i, p in enumerate(picks, 1):
        conf = round(p.get("confidence", 0) * 100)
        if p.get("type") == "prop":
            tag = "🏟️" if p.get("is_team_prop") else "👤"
            lines.append(
                f"`LEG {i}`  {tag} **{p['player']}**\n"
                f"┣  {p['stat']} **{p['direction']} {p['line']}**\n"
                f"┗  Conf **{conf}%**"
            )
        else:
            mkt = _MARKET.get(p.get("market", ""), p.get("market", "").upper())
            fmt_odds = (lambda v: f"+{v}" if isinstance(v, (int, float)) and v > 0 else str(v))(p.get("best_odds", ""))
            lines.append(
                f"`LEG {i}`  **{p.get('away_team', '')} @ {p.get('home_team', '')}**\n"
                f"┣  {mkt}  **{p.get('selection', '')}**  `{fmt_odds}`\n"
                f"┗  Conf **{conf}%**"
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


def _alert_starting_soon(slip: dict, pick: dict) -> None:
    gt   = _fmt_time(pick.get("commence_time", ""))
    name = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
    _post_embed({
        "title":       f"🔔  GAME STARTING SOON",
        "description": (
            f"{_ticket_header(slip)}\n"
            f"**{name}**  tips off in ~30 min  ·  🕐 **{gt}**\n\n"
            f"{_slip_legs(slip['picks'])}"
        ),
        "color": 0xF9A825,
        "footer": {"text": "⏱️ Get your slip in before tip-off"},
    })


def _alert_live(slip: dict, pick: dict) -> None:
    name = pick.get("player") or f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}"
    _post_embed({
        "title":       f"🔴  LIVE — {name}",
        "description": (
            f"{_ticket_header(slip)}\n"
            f"Game is **LIVE** — slip is active  🎯\n\n"
            f"{_slip_legs(slip['picks'])}"
        ),
        "color": 0xE53935,
        "footer": {"text": "Tracking result — updates when game ends"},
    })


def _alert_result(slip: dict, result: str, ratio: dict) -> None:
    platform = _platform_label(slip["platform"])
    w, l, p  = ratio["wins"], ratio["losses"], ratio.get("pushes", 0)
    total    = w + l
    pct_str  = f"  ·  {round(w / total * 100)}% win rate" if total > 0 else ""
    record   = f"{w}W – {l}L{' – ' + str(p) + 'P' if p else ''}{pct_str}"

    if result == "cashed":
        title  = "✅  SLIP CASHED"
        stamp  = "W I N N E R"
        color  = 0x1B5E20
        footer = f"🎉 All legs hit · {platform} · Record: {record}"
    elif result == "dead":
        title  = "❌  SLIP DEAD"
        stamp  = "L O S T"
        color  = 0xB71C1C
        footer = f"💔 A leg missed · {platform} · Record: {record}"
    else:
        title  = "➖  SLIP PUSH"
        stamp  = "P U S H"
        color  = 0x607D8B
        footer = f"No result · {platform} · Record: {record}"

    _post_embed({
        "title":       title,
        "description": (
            f"{_ticket_header(slip)}\n"
            f"```\n"
            f"  *** {stamp} ***\n"
            f"```\n"
            f"{_slip_legs(slip['picks'])}\n\n"
            f"📊  **Record:**  {record}"
        ),
        "color": color,
        "footer": {"text": footer},
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
                # Dedup by GAME not slip — multiple slips covering same game = 1 alert
                _game_id = pick.get('event_id') or pick.get('game_key') or f"{pick.get('home_team','')}:{pick.get('away_team','')}"
                soon_key = f"game:soon:{_game_id}"
                if 25 <= mins_to_game <= 35 and not _alerted(r, soon_key):
                    _alert_starting_soon(slip, pick)
                    _mark_alerted(r, soon_key)
                    alerts_fired += 1

                # ── Live now (0-5 min past kick-off) ─────────────────────
                live_key = f"game:live:{_game_id}"
                if -5 <= mins_to_game <= 2 and not _alerted(r, live_key):
                    _alert_live(slip, pick)
                    _mark_alerted(r, live_key)
                    alerts_fired += 1

                # ── Result check (game should be done) ───────────────────
                if mins_to_game < -150:   # game started 2.5h ago — enough for any sport
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)

            # ── Settle slip only when EVERY leg has a result ──────────────
            # Count legs that are old enough to have finished
            settled_picks = [
                p for p in picks
                if _parse_time(p.get("commence_time", "")) and
                (_now_utc() - _parse_time(p.get("commence_time", ""))).total_seconds() > 150 * 60
            ]
            # Only settle if ALL legs — not just some — have returned a result
            if results and len(results) == len(picks) and len(settled_picks) == len(picks):
                if "lost" in results:
                    slip_result = "dead"
                elif all(r == "won" for r in results):
                    slip_result = "cashed"
                else:
                    slip_result = "push"

                # Dedup result alert by game-day period, not slip_id
                # so duplicate slips don't fire multiple CASHED/DEAD alerts
                _period_date = f"{slip.get('period','night')}:{slip.get('platform','hardrock')}:{slip.get('created','')[:10]}"
                result_key = f"game:result:{_period_date}"
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
