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
    yesterday = (et_naive() - __import__('datetime').timedelta(days=1)).strftime("%Y-%m-%d")
    removed   = 0
    for sid, raw in all_slips.items():
        # Valid IDs look like "day:hardrock:2026-06-13"
        if not re.match(r"^(day|night):[a-z]+:\d{4}-\d{2}-\d{2}$", sid):
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged ghost slip: %s", sid)
        elif sid.split(":")[-1] not in (today, yesterday):
            r.hdel(_SLIP_KEY, sid)
            removed += 1
            logger.info("Purged stale slip: %s", sid)
    return removed


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


def _slip_legs(picks: list[dict], results: list[str] | None = None) -> str:
    """Render each leg in slip format. Pass results=['won','lost',...] to show outcome per leg."""
    _MARKET = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}
    _OUTCOME = {"won": "✅  WON", "lost": "❌  LOST", "push": "➖  PUSH"}
    lines = []
    for i, p in enumerate(picks, 1):
        conf    = round(p.get("confidence", 0) * 100)
        outcome = _OUTCOME.get((results[i - 1] if results and i <= len(results) else ""), "")
        outcome_line = f"\n┗  {outcome}" if outcome else ""

        if p.get("type") == "prop":
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
            f"{_slip_legs(slip['picks'], results)}\n\n"
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
                        # Draw: in soccer a draw on a team ML pick = lost
                        # Only true push is if the book explicitly offers draw markets
                        sport = pick.get("sport_key", "")
                        if "soccer" in sport or "football" in sport:
                            return "lost"
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

        now = _now_utc()
        alerts_fired = 0

        # ── Pass 1: collect soon/live — separated by DAY vs NIGHT ──────────────
        # soon window: 0–10 min before tip-off
        day_soon:   list[str] = []
        night_soon: list[str] = []
        day_live:   list[str] = []
        night_live: list[str] = []

        for slip in slips:
            plat   = _platform_label(slip["platform"])
            period = slip.get("period", "day")

            for pick in slip.get("picks", []):
                ct = _parse_time(pick.get("commence_time", ""))
                if not ct:
                    continue
                mins = (ct - now).total_seconds() / 60
                gid  = (pick.get("event_id") or pick.get("game_key") or
                        f"{pick.get('home_team','')}:{pick.get('away_team','')}")
                name = (pick.get("player") or pick.get("title") or pick.get("question") or
                        f"{pick.get('away_team', '')} @ {pick.get('home_team', '')}").strip(" @")
                gt   = _fmt_time(pick.get("commence_time", ""))
                tag  = f"`[{plat}]`"

                soon_key = f"game:soon:{gid}"
                if 0 <= mins <= 10 and not _alerted(r, soon_key):
                    line = f"**{name}**  ·  🕐 {gt}  {tag}"
                    (day_soon if period == "day" else night_soon).append(line)
                    _mark_alerted(r, soon_key)

                live_key = f"game:live:{gid}"
                if -5 <= mins <= 2 and not _alerted(r, live_key):
                    line = f"🔴 **{name}**  {tag}"
                    (day_live if period == "day" else night_live).append(line)
                    _mark_alerted(r, live_key)

        if day_soon:
            _post_embed({
                "title":       "🔔  DAY GAMES STARTING NOW",
                "description": "\n".join(day_soon),
                "color":       0xF9A825,
                "footer":      {"text": "⏱️ Last chance — tip-off in under 10 min"},
            })
            alerts_fired += 1
        if night_soon:
            _post_embed({
                "title":       "🔔  NIGHT GAMES STARTING NOW",
                "description": "\n".join(night_soon),
                "color":       0xF9A825,
                "footer":      {"text": "⏱️ Last chance — tip-off in under 10 min"},
            })
            alerts_fired += 1
        if day_live:
            _post_embed({
                "title":       "🔴  DAY GAMES NOW LIVE",
                "description": "\n".join(day_live),
                "color":       0xE53935,
                "footer":      {"text": "Tracking results — updates when games end"},
            })
            alerts_fired += 1
        if night_live:
            _post_embed({
                "title":       "🔴  NIGHT GAMES NOW LIVE",
                "description": "\n".join(night_live),
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
                if not ct:
                    continue
                mins = (ct - now).total_seconds() / 60
                if mins < -150:
                    res = _check_pick_result(pick)
                    if res:
                        results.append(res)

            # Only settle when ALL legs have a result and are old enough
            settled_picks = [
                p for p in picks
                if _parse_time(p.get("commence_time", "")) and
                (_now_utc() - _parse_time(p.get("commence_time", ""))).total_seconds() > 150 * 60
            ]
            if results and len(results) == len(picks) and len(settled_picks) == len(picks):
                if "lost" in results:
                    slip_result = "dead"
                elif all(rv == "won" for rv in results):
                    slip_result = "cashed"
                else:
                    slip_result = "push"

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
