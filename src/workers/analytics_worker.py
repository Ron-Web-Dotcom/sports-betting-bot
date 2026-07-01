"""Analytics worker — daily/weekly summaries, self-improvement, portfolio snapshots."""
import logging
from datetime import date as _date

logger = logging.getLogger(__name__)

# Bot launch date — used to display "Week 1", "Week 2", etc. (7-day rolling weeks)
_BOT_LAUNCH = _date(2026, 6, 9)

# ── Sleep window: 3 AM – 5 AM Eastern ─────────────────────────────────────────

def is_sleep_time() -> bool:
    """Return True if current Eastern time is inside the 3 AM–5 AM sleep window."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


def _load_slip_results(days_back: int = 1) -> tuple[int, int, int, list, list]:
    """
    Load settled slip results from Redis for the last N days.
    Returns (wins, losses, pushes, winner_slips, loser_slips).
    """
    import json
    from datetime import datetime, timedelta
    import zoneinfo
    from src.core.config import REDIS_URL
    import redis as _redis

    ET    = zoneinfo.ZoneInfo("America/New_York")
    now_et = datetime.now(ET)
    cutoff = (now_et - timedelta(days=days_back)).date()
    today  = now_et.date()

    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw_slips = r.hgetall("slips:active")
        ratio_raw = r.hgetall("slips:ratio")
    except Exception:
        return 0, 0, 0, [], []

    winner_slips: list = []
    loser_slips:  list = []

    for raw in raw_slips.values():
        try:
            slip = json.loads(raw)
            created_date = slip.get("created", "")[:10]
            # Exclude slips before cutoff AND slips from today (avoid double-counting)
            if created_date < str(cutoff) or created_date >= str(today):
                continue
            status = slip.get("status", "active")
            if status == "cashed":
                winner_slips.append(slip)
            elif status == "dead":
                loser_slips.append(slip)
        except Exception:
            pass

    # Count wins/losses from date-filtered slips (not all-time slips:ratio)
    wins   = len(winner_slips)
    losses = len(loser_slips)
    pushes = int(ratio_raw.get("pushes", 0))

    return wins, losses, pushes, winner_slips, loser_slips


def _slip_summary_line(slip: dict) -> str:
    """One-line description of a slip for recap embeds."""
    _PLATFORM = {"hardrock": "HardRock", "kalshi": "Kalshi"}
    platform = _PLATFORM.get(slip.get("platform", ""), slip.get("platform", "").title())
    period   = slip.get("period", "").upper()
    picks    = slip.get("picks") or []
    n        = len(picks)

    def _pick_label(p: dict) -> str:
        # Kalshi pick: use shortened question
        if p.get("question"):
            q = p["question"]
            return q[:40] + "…" if len(q) > 40 else q
        return p.get("selection") or p.get("player") or "?"

    legs = ", ".join(_pick_label(p) for p in picks[:2])
    return f"• **{platform} {period}** — {n}-leg: {legs}"


def send_daily_summary():
    """No-op — daily summary is now combined into enter_sleep_mode at 3 AM."""
    logger.info("send_daily_summary: skipped — combined into enter_sleep_mode")
    return {"skipped": "combined_into_enter_sleep_mode"}


def _send_daily_summary_impl():
    """
    Internal: posts the daily summary after the last game of the day has completed.
    Called internally if needed; public entry point is enter_sleep_mode at 3 AM.
    """
    import zoneinfo
    from datetime import datetime, timedelta
    ET = zoneinfo.ZoneInfo("America/New_York")
    now_et = datetime.now(ET)

    # Find the latest scheduled game start time today from Redis cache
    last_game_et = None
    try:
        from src.core.config import REDIS_URL
        import redis as _redis, json
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        night_raw = r.get("sofascore:night_games")
        day_raw   = r.get("sofascore:day_games")
        all_games = (json.loads(night_raw) if night_raw else []) + (json.loads(day_raw) if day_raw else [])
        if all_games:
            from dateutil.parser import parse as _parse
            times = []
            for ev in all_games:
                ct = ev.get("commence_time", "")
                try:
                    t = _parse(ct).astimezone(ET) if ct else None
                    if t:
                        times.append(t)
                except Exception:
                    pass
            if times:
                last_start = max(times)
                # Add ~3 hours for game duration
                last_game_et = last_start + timedelta(hours=3)
    except Exception:
        pass

    # Guard: only post once per calendar day
    _r = None
    _day_key = f"daily_summary_sent:{now_et.strftime('%Y-%m-%d')}"
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        if _r.get(_day_key):
            logger.info("send_daily_summary: already sent today")
            return {"skipped": "already_sent_today"}
    except Exception:
        pass

    # If last game hasn't finished yet, skip — runner will retry next hour
    if last_game_et and now_et < last_game_et:
        logger.info("send_daily_summary: last game ends ~%s ET — not ready yet (now %s ET)",
                    last_game_et.strftime("%I:%M %p"), now_et.strftime("%I:%M %p"))
        return {"skipped": "games_not_done_yet", "retry_after": last_game_et.isoformat()}

    # Pull results from Redis slip tracker
    wins, losses, pushes, winner_slips, loser_slips = _load_slip_results(days_back=1)
    total = wins + losses

    pct_str = f" ({round(wins/total*100)}% hit rate)" if total > 0 else ""
    record  = f"{wins}W – {losses}L{pct_str}" if total > 0 else "No settled slips yet"

    win_lines  = "\n".join(_slip_summary_line(s) for s in winner_slips) or "—"
    loss_lines = "\n".join(_slip_summary_line(s) for s in loser_slips)  or "—"

    import hashlib
    date_str = now_et.strftime("%b %-d, %Y")
    slip_id  = hashlib.md5(f"daily{date_str}".encode()).hexdigest()[:8].upper()
    color    = 0x1B5E20 if wins >= losses and total > 0 else (0xB71C1C if losses > wins else 0x607D8B)

    embed = {
        "title":       f"📊  DAILY SUMMARY  ·  {now_et.strftime('%A, %b %-d')}",
        "description": (
            f"**{date_str}**  ·  Slip `#{slip_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "color": color,
        "fields": [
            {"name": "TODAY'S RECORD", "value": record,    "inline": False},
            {"name": "✅  CASHED",     "value": win_lines,  "inline": True},
            {"name": "❌  DEAD",       "value": loss_lines, "inline": True},
            {"name": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
             "value": "☀️  Day entry  **10:30 AM ET**  ·  🌙  Night entry  **4:30 PM ET**",
             "inline": False},
        ],
        "footer": {"text": f"Daily recap  ·  {now_et.strftime('%-I:%M %p ET')}  ·  {date_str}"},
    }

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    _run_async(_post({"embeds": [embed]}))

    # Mark as sent so retries at 11 PM / 12:30 AM are no-ops
    try:
        if _r:
            _r.setex(_day_key, 86400, "1")
    except Exception:
        pass

    logger.info("Daily summary sent")
    return {"sent": True, "wins": wins, "losses": losses, "pushes": pushes}


def send_weekly_summary():
    """Fires Sunday midnight Eastern — last week recap + new week fresh start in one embed."""
    import hashlib, zoneinfo
    from datetime import datetime
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post

    ET = zoneinfo.ZoneInfo("America/New_York")
    now_et = datetime.now(ET)

    # Only fire on Sunday (weekday 6) — prevents accidental daily firing
    if now_et.weekday() != 6:
        logger.info("send_weekly_summary: skipping — not Sunday (weekday=%d)", now_et.weekday())
        return {"skipped": "not_sunday"}

    # Guard: only post once per week
    _r = None
    _week_key = f"weekly_summary_sent:{now_et.strftime('%Y-W%W')}"
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        if _r.get(_week_key):
            logger.info("send_weekly_summary: already sent this week")
            return {"skipped": "already_sent_this_week"}
    except Exception:
        pass

    wins, losses, pushes, winner_slips, loser_slips = _load_slip_results(days_back=7)
    total = wins + losses

    pct_str = f" ({round(wins/total*100)}% hit rate)" if total > 0 else ""
    record  = f"{wins}W – {losses}L{pct_str}" if total > 0 else "No settled slips this week"

    win_lines  = "\n".join(_slip_summary_line(s) for s in winner_slips[:5]) or "—"
    loss_lines = "\n".join(_slip_summary_line(s) for s in loser_slips[:5])  or "—"

    # Sport breakdown
    sport_stats: dict = {}
    for slip in winner_slips + loser_slips:
        for pick in slip.get("picks", []):
            sk = pick.get("sport_key") or pick.get("sport") or ("kalshi" if pick.get("question") else "other")
            if sk not in sport_stats:
                sport_stats[sk] = {"w": 0, "l": 0}
            if slip.get("status") == "cashed":
                sport_stats[sk]["w"] += 1
            else:
                sport_stats[sk]["l"] += 1
    sport_lines = [f"{sk.split('_')[-1].upper()}: {v['w']}W-{v['l']}L" for sk, v in sport_stats.items()]
    sport_breakdown = "  ".join(sport_lines) or "—"

    week_num  = ((now_et.date() - _BOT_LAUNCH).days // 7) + 1
    next_week = week_num + 1
    date_str = now_et.strftime("%b %-d, %Y")
    slip_id  = hashlib.md5(f"weekly{date_str}".encode()).hexdigest()[:8].upper()
    color    = 0x1B5E20 if wins >= losses and total > 0 else (0xB71C1C if losses > wins else 0x607D8B)

    embed = {
        "title": f"📊  WEEK {week_num} RECAP  ·  🟢  WEEK {next_week} STARTS NOW",
        "description": (
            f"**{date_str}**  ·  Slip `#{slip_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "color": color,
        "fields": [
            {"name": "WEEK'S RECORD",  "value": record,          "inline": False},
            {"name": "TOTAL SLIPS",    "value": str(total),      "inline": True},
            {"name": "BY SPORT",       "value": sport_breakdown,  "inline": True},
            {"name": "​",        "value": "​",          "inline": True},
            {"name": "✅  WINNERS",    "value": win_lines,        "inline": True},
            {"name": "❌  LOSERS",     "value": loss_lines,       "inline": True},
            {"name": "​",        "value": "​",          "inline": True},
            {"name": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
             "value": (
                 f"Fresh slate  ·  Record resets  ·  Let's build the streak 🎯\n"
                 f"☀️  Day entry  **10:30 AM ET**  ·  🌙  Night entry  **4:30 PM ET**"
             ),
             "inline": False},
        ],
        "footer": {"text": f"Week {week_num} closed  ·  Week {next_week} open  ·  {date_str}"},
    }

    _run_async(_post({"embeds": [embed]}))

    try:
        if _r:
            _r.setex(_week_key, 7 * 86400, "1")
    except Exception:
        pass

    logger.info("Weekly summary + new week sent: %dW-%dL-%dP", wins, losses, pushes)
    return {"wins": wins, "losses": losses, "pushes": pushes, "total": total}


def send_weekly_fresh_start():
    """No-op — new week message is now combined into send_weekly_summary."""
    return {"skipped": "combined_into_weekly_summary"}


def send_monthly_summary():
    from src.engines.summary_engine import get_monthly_summary
    from src.core.timezone import et_naive

    now = et_naive()
    # Runs on the 1st of the month — summarize the PREVIOUS month
    prev_month = now.month - 1 if now.month > 1 else 12
    prev_year  = now.year if now.month > 1 else now.year - 1
    monthly = get_monthly_summary(year=prev_year, month=prev_month)

    _roi    = monthly.get('total_roi', 0) or 0
    _pnl    = monthly.get('total_profit', 0) or 0
    _clv    = monthly.get('avg_clv', 0) or 0
    roi_str   = f"{_roi:.1%}"
    color     = 0x1B5E20 if _pnl >= 0 else 0xB71C1C

    embed_body = (
        f"─────────────────────────\n"
        f"**Bets:**  {monthly.get('total_bets', 0)}  ·  "
        f"**P&L:**  {_pnl:+.2f}u  ·  "
        f"**ROI:**  {roi_str}\n\n"
        f"**Avg CLV:**  {_clv:+.2%}\n"
        f"**Best Sport:**  {monthly.get('best_sport') or '—'}  ·  "
        f"**Best Market:**  {monthly.get('best_market') or '—'}\n\n"
        f"🔥 Win Streak: **{monthly.get('largest_winning_streak', 0)}**  ·  "
        f"💔 Loss Streak: **{monthly.get('largest_losing_streak', 0)}**"
    )

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    _run_async(_post({"embeds": [_embed(
        title=f"🗓️  Monthly Summary  ·  {_date(prev_year, prev_month, 1).strftime('%B %Y')}",
        description=embed_body,
        color=color,
    )]}))

    logger.info("Monthly summary sent")
    return {"sent": True, "stats": monthly}


def enter_sleep_mode():
    """3 AM Eastern — settle any remaining picks, post goodnight with W/L/P summary."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from src.engines.self_improvement_engine import run_full_self_improvement
    from datetime import datetime
    import zoneinfo

    # Dedup: only post once per calendar day even if scheduler fires twice at 3 AM
    try:
        from src.core.config import REDIS_URL
        import redis as _redis_sleep
        _r_sleep = _redis_sleep.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _sleep_key = f"sleep_mode_sent:{datetime.now(zoneinfo.ZoneInfo('America/New_York')).strftime('%Y-%m-%d')}"
        if not _r_sleep.set(_sleep_key, "1", ex=86400, nx=True):
            logger.info("enter_sleep_mode: already ran today — skipping duplicate")
            return {"skipped": "already_ran_today"}
    except Exception:
        pass  # Redis down — allow through

    # Settle any late-finishing games before posting the nightly summary
    try:
        from src.workers.settlement_worker import settle_completed_picks as _settle
        _settle()
        logger.info("Pre-sleep settlement complete")
    except Exception as _se:
        logger.warning("Pre-sleep settlement failed: %s", _se)

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # Pull today's settled results from Redis slip tracker
    wins, losses, pushes, winner_slips, loser_slips = _load_slip_results(days_back=1)
    total = wins + losses

    if total == 0:
        record_str = "No settled picks today — results appear as games finish."
        color = 0x1A237E
    else:
        pct = round(wins / total * 100) if total else 0
        record_str = f"**{wins}W – {losses}L** ({pct}% hit rate)"
        color = 0x1A237E

    win_text  = "\n".join(_slip_summary_line(s) for s in winner_slips[:5]) or "—"
    loss_text = "\n".join(_slip_summary_line(s) for s in loser_slips[:5])  or "—"

    # ── Weekly summary — Sunday night only (fires 3 AM Monday morning) ───────
    if et.weekday() == 0:  # 0 = Monday; 3 AM Monday = Sunday night rollover
        try:
            # Shared dedup key with send_weekly_summary — only one of them posts per week
            _week_key = f"weekly_summary_sent:{et.strftime('%Y-W%W')}"
            from src.core.config import REDIS_URL as _RURL_W
            import redis as _redis_w
            _r_w = _redis_w.from_url(_RURL_W, decode_responses=True, socket_connect_timeout=2)
            if _r_w.get(_week_key):
                logger.info("enter_sleep_mode: weekly summary already sent this week — skipping")
            else:
                w_wins, w_losses, w_pushes, w_winners, w_losers = _load_slip_results(days_back=7)
                w_total  = w_wins + w_losses
                w_pct    = f" ({round(w_wins/w_total*100)}% hit rate)" if w_total > 0 else ""
                w_record = f"{w_wins}W – {w_losses}L{w_pct}" if w_total > 0 else "No settled slips this week"
                w_win_lines  = "\n".join(_slip_summary_line(s) for s in w_winners[:5]) or "—"
                w_loss_lines = "\n".join(_slip_summary_line(s) for s in w_losers[:5])  or "—"
                w_sport: dict = {}
                for slip in w_winners + w_losers:
                    for pick in slip.get("picks", []):
                        sk = pick.get("sport_key") or pick.get("sport") or ("kalshi" if pick.get("question") else "other")
                        if sk not in w_sport:
                            w_sport[sk] = {"w": 0, "l": 0}
                        w_sport[sk]["w" if slip.get("status") == "cashed" else "l"] += 1
                w_sport_lines = [f"{sk.split('_')[-1].upper()}: {v['w']}W-{v['l']}L" for sk, v in w_sport.items()]
                import hashlib as _hl
                week_num  = ((et.date() - _BOT_LAUNCH).days // 7) + 1
                prev_week = max(week_num - 1, 1)
                w_slip_id = _hl.md5(f"weekly{et.strftime('%Y-W%W')}".encode()).hexdigest()[:8].upper()
                w_color   = 0x1B5E20 if w_wins >= w_losses and w_total > 0 else (0xB71C1C if w_losses > w_wins else 0x607D8B)
                weekly_embed = {
                    "title": f"📊  WEEK {prev_week} SUMMARY  ·  🟢  WEEK {week_num} STARTS NOW",
                    "description": (
                        f"**{et.strftime('%b %-d, %Y')}**  ·  Slip `#{w_slip_id}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    ),
                    "color": w_color,
                    "fields": [
                        {"name": "WEEK'S RECORD", "value": w_record,                              "inline": False},
                        {"name": "TOTAL SLIPS",   "value": str(w_total),                          "inline": True},
                        {"name": "BY SPORT",      "value": "  ".join(w_sport_lines) or "—",       "inline": True},
                        {"name": "​",             "value": "​",                                   "inline": True},
                        {"name": "✅  WINNERS",   "value": w_win_lines,                           "inline": True},
                        {"name": "❌  LOSERS",    "value": w_loss_lines,                          "inline": True},
                        {"name": "​",             "value": "​",                                   "inline": True},
                    ],
                    "footer": {"text": f"Week {prev_week} closed  ·  Week {week_num} open  ·  {et.strftime('%b %-d, %Y')}"},
                }
                _run_async(_post({"embeds": [weekly_embed]}))
                _r_w.setex(_week_key, 7 * 86400, "1")
                logger.info("Weekly summary posted Sunday night: %dW-%dL-%dP", w_wins, w_losses, w_pushes)
        except Exception as _we:
            logger.warning("3 AM weekly summary failed: %s", _we)

    # Run self-improvement silently while sleeping
    try:
        run_full_self_improvement()
        logger.info("Self-improvement ran during sleep window")
    except Exception as e:
        logger.warning("Self-improvement failed during sleep: %s", e)

    import hashlib
    date_str = et.strftime("%b %-d, %Y")
    slip_id  = hashlib.md5(f"daily{date_str}".encode()).hexdigest()[:8].upper()
    color    = 0x1B5E20 if wins > losses and total > 0 else (0xB71C1C if losses > wins and total > 0 else 0x1A237E)

    embed = {
        "title": f"📊  DAILY SUMMARY  ·  {et.strftime('%A, %b %-d')}",
        "description": (
            f"**{date_str}**  ·  Slip `#{slip_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "color": color,
        "fields": [
            {"name": "TODAY'S RECORD", "value": record_str,  "inline": True},
            {"name": "TOTAL PICKS",    "value": str(total),  "inline": True},
            {"name": "​",         "value": "​",     "inline": True},
            {"name": "✅  CASHED",     "value": win_text,    "inline": True},
            {"name": "❌  DEAD",       "value": loss_text,   "inline": True},
            {"name": "​",         "value": "​",     "inline": True},
            {
                "name":  "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "value": (
                    f"☀️  Day entry  **10:30 AM ET**  ·  🌙  Night entry  **4:30 PM ET**\n"
                    f"Scanning paused  ·  Back online at 5 AM ET"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": f"Daily recap  ·  3:00 AM ET  ·  {date_str}  ·  See you at 5 AM"},
    }
    _run_async(_post({"embeds": [embed]}))
    logger.info("Sleep mode entered at %s ET", et.strftime("%H:%M"))
    return {"sleep_entered": et.isoformat(), "wins": wins, "losses": losses, "pushes": pushes}


def wake_up_brief():
    """5 AM Eastern — slip-style wake-up post."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    from datetime import datetime
    import zoneinfo, hashlib

    ET      = zoneinfo.ZoneInfo("America/New_York")
    now_et  = datetime.now(ET)
    date_str = now_et.strftime("%b %-d, %Y")
    slip_id  = hashlib.md5(f"wakeup{date_str}".encode()).hexdigest()[:8].upper()

    embed = {
        "title": "🟢  BOT IS LIVE  ·  Let's Get It",
        "description": (
            f"**{date_str}**  ·  Slip `#{slip_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "fields": [
            {"name": "STATUS",      "value": "🟢  Scanning all sports",                    "inline": True},
            {"name": "MODE",        "value": "High-confidence picks only",                  "inline": True},
            {"name": "​", "value": "​",                                            "inline": True},
            *([
                {"name": "☀️  DAY ENTRY",   "value": "**10:30 AM ET**  ·  Kalshi only  ·  HardRock resumes Jul 10", "inline": True},
                {"name": "🌙  NIGHT ENTRY", "value": "**4:30 PM ET**  ·  Kalshi only  ·  HardRock resumes Jul 10",  "inline": True},
            ] if __import__('datetime').date.today() < __import__('datetime').date(2026, 7, 10) else [
                {"name": "☀️  DAY ENTRY",   "value": "**10:30 AM ET**  ·  HardRock + Kalshi", "inline": True},
                {"name": "🌙  NIGHT ENTRY", "value": "**4:30 PM ET**  ·  HardRock + Kalshi",  "inline": True},
            ]),
            {"name": "​", "value": "​",                                            "inline": True},
            {"name": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
             "value": "Picks post automatically when edge is found.\nAll picks require positive EV + 77%+ confidence.",
             "inline": False},
        ],
        "color": 0x00C851,
        "footer": {"text": f"5:00 AM ET  ·  {date_str}  ·  Bot online"},
    }
    _run_async(_post({"embeds": [embed]}))
    logger.info("Wake-up brief sent (5 AM)")
    return {"woke_up": True}


def run_self_improvement():
    from src.engines.self_improvement_engine import run_full_self_improvement
    result = run_full_self_improvement()
    logger.info("Self-improvement cycle done: %s periods", len(result or []))
    return result


def snapshot_portfolio():
    from src.engines.portfolio_engine import get_performance_stats
    from src.db.session import get_db
    from src.db.models import BankrollSnapshot
    from src.core.timezone import et_naive

    stats = get_performance_stats("lifetime")
    daily_stats = get_performance_stats("daily")
    with get_db() as db:
        db.add(BankrollSnapshot(
            recorded_at  = et_naive(),
            balance      = stats.get("net_units", 0),
            units_total  = daily_stats.get("net_units", 0),
            note         = "auto",
        ))
    logger.info("Portfolio snapshot saved")
    return stats


def check_odds_api_restored():
    """
    Runs on the 10th of each month at 7 AM ET.
    Tests whether Odds API credits have been restored — posts a Discord alert if so.
    """
    from src.engines.odds_engine import _get, _clear_credits_exhausted
    import zoneinfo
    from datetime import datetime

    ET  = zoneinfo.ZoneInfo("America/New_York")
    now = datetime.now(ET)

    # Try a lightweight /sports call (1 credit) to confirm API is back
    try:
        import httpx
        from src.core.config import ODDS_API_KEY, ODDS_API_BASE
        r = httpx.get(f"{ODDS_API_BASE}/sports", params={"apiKey": ODDS_API_KEY, "all": "false"}, timeout=10)
        if r.status_code == 200:
            _clear_credits_exhausted()
            remaining = r.headers.get("x-requests-remaining", "?")
            msg = (
                f"✅ **Odds API credits restored!**\n"
                f"Credits remaining: **{remaining}**\n"
                f"Odds scanning resumes automatically — picks will post again today."
            )
            from src.discord_bot.bot import _post
            from src.workers.alert_worker import _run_async
            _run_async(_post({"embeds": [{"title": "✅ Odds API Restored", "description": msg, "color": 0x1B5E20}]}))
            logger.info("Odds API restored: %s credits remaining", remaining)
            return {"status": "restored", "credits_remaining": remaining}
        elif r.status_code in (401, 403):
            logger.info("check_odds_api_restored: still exhausted (month=%d day=%d)", now.month, now.day)
            return {"status": "still_exhausted"}
        else:
            logger.warning("check_odds_api_restored: unexpected status %s", r.status_code)
            return {"status": "unknown", "http": r.status_code}
    except Exception as e:
        logger.warning("check_odds_api_restored failed: %s", e)
        return {"status": "error", "error": str(e)}


def cleanup_old_slips():
    """
    Runs nightly at 2:52 AM ET — removes slips older than 2 days from Redis.
    Slips are already ignored by summaries after their date window, so this
    is purely a housekeeping step with no effect on reporting.
    """
    import json
    from datetime import datetime, timedelta
    import zoneinfo
    from src.core.config import REDIS_URL
    import redis as _redis

    ET     = zoneinfo.ZoneInfo("America/New_York")
    cutoff = (datetime.now(ET) - timedelta(days=2)).strftime("%Y-%m-%d")

    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        all_slips = r.hgetall("slips:active")
    except Exception as e:
        logger.warning("cleanup_old_slips: Redis error: %s", e)
        return {"removed": 0}

    import re as _re_slip
    removed = 0
    for sid, raw in all_slips.items():
        try:
            date_part = sid.split(":")[-1]   # e.g. "2026-06-12" from "day:hardrock:2026-06-12"
            if not _re_slip.match(r'^\d{4}-\d{2}-\d{2}$', date_part):
                continue  # not a date-keyed slip — don't delete
            if date_part < cutoff:
                r.hdel("slips:active", sid)
                removed += 1
        except Exception:
            pass

    logger.info("cleanup_old_slips: removed %d stale slips (cutoff %s)", removed, cutoff)
    return {"removed": removed, "cutoff": cutoff}


def flush_memory():
    """
    Run just before sleep mode (2:55 AM ET) to free RAM:
    - Vacuum systemd journal down to 50 MB
    - Flush Redis expired keys
    - Force Python garbage collection
    Keeps everything running — just clears accumulated waste.
    """
    import gc
    import subprocess

    freed = {}

    # 1. Vacuum systemd journal — logs pile up silently and eat RAM
    try:
        result = subprocess.run(
            ["journalctl", "--vacuum-size=50M"],
            capture_output=True, text=True, timeout=30,
        )
        freed["journal"] = "vacuumed to 50MB"
        logger.info("flush_memory: journal vacuumed")
    except Exception as e:
        logger.warning("flush_memory: journal vacuum failed: %s", e)

    # 2. Redis — remove all expired keys immediately
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        # DEBUG SLEEP forces Redis to purge expired keys now rather than lazily
        info = r.info("memory")
        freed["redis_used_mb"] = round(info.get("used_memory", 0) / 1024 / 1024, 1)
        logger.info("flush_memory: Redis using %s MB", freed["redis_used_mb"])
    except Exception as e:
        logger.warning("flush_memory: Redis flush failed: %s", e)

    # 3. Python garbage collection — clears circular refs and unreferenced objects
    collected = gc.collect()
    freed["gc_collected"] = collected
    logger.info("flush_memory: GC collected %d objects", collected)

    logger.info("flush_memory complete: %s", freed)
    return freed


def cleanup_old_snapshots():
    """Delete OddsSnapshots older than 7 days — prevents unbounded table growth.

    Without cleanup: ~6.9M rows/day accumulate and eventually kill query performance.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, LineMovement, AlertRecord
    from datetime import timedelta
    from src.core.timezone import et_naive

    cutoff_snapshots     = et_naive() - timedelta(days=7)
    cutoff_alerts        = et_naive() - timedelta(days=30)
    cutoff_line_movement = et_naive() - timedelta(days=90)
    cutoff_picks         = et_naive() - timedelta(days=365)

    with get_db() as db:
        from src.db.models import Pick, Game

        deleted_snaps = db.query(OddsSnapshot).filter(
            OddsSnapshot.captured_at < cutoff_snapshots
        ).delete(synchronize_session=False)

        deleted_alerts = db.query(AlertRecord).filter(
            AlertRecord.sent_at < cutoff_alerts
        ).delete(synchronize_session=False)

        deleted_movements = db.query(LineMovement).filter(
            LineMovement.detected_at < cutoff_line_movement
        ).delete(synchronize_session=False)

        # Keep picks forever for win-rate tracking — only purge after 1 year
        deleted_picks = db.query(Pick).filter(
            Pick.generated_at < cutoff_picks
        ).delete(synchronize_session=False)

    logger.info(
        "Cleanup: %d snapshots, %d alerts, %d line movements, %d picks (>1yr)",
        deleted_snaps, deleted_alerts, deleted_movements, deleted_picks,
    )
    return {
        "snapshots_deleted":     deleted_snaps,
        "alerts_deleted":        deleted_alerts,
        "movements_deleted":     deleted_movements,
        "picks_deleted":         deleted_picks,
    }


def health_check():
    """Post a single status card to Discord once per hour — confirms bot is alive."""
    if is_sleep_time():
        return {"skipped": "sleep_mode"}

    try:
        from src.core.config import REDIS_URL, DISCORD_WEBHOOK_URL
        from src.discord_bot.bot import _post
        import redis as _redis, json
        from datetime import datetime
        import zoneinfo

        if not DISCORD_WEBHOOK_URL:
            return {"skipped": "no_webhook"}

        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # Prop counts from cache
        odds_raw = r.get("props:odds_api")
        odds_props = json.loads(odds_raw) if odds_raw else []
        kalshi_raw = r.get("kalshi:live_markets")
        kalshi_props = json.loads(kalshi_raw) if kalshi_raw else []
        # Count active picks from slip tracker
        slips_raw = r.hgetall("slips:active")
        active_slips = []
        for _sv in (slips_raw.values() if slips_raw else []):
            try:
                _sd = json.loads(_sv)
                if _sd.get("status") == "active":
                    active_slips.append(_sd)
            except Exception:
                pass
        picks_count = len(active_slips)

        et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        time_str = et.strftime("%I:%M %p ET")

        embed = {
            "title": "🟢 Bot Online",
            "description": (
                f"Scanning odds every 10 min · Props every 20 min · Picks every 20 min.\n"
                f"Top picks posted when high-confidence edges are found."
            ),
            "color": 0x00C851,
            "fields": [
                {"name": "Odds API Props",  "value": f"{len(odds_props):,}", "inline": True},
                {"name": "Kalshi",          "value": f"{len(kalshi_props):,}", "inline": True},
                {"name": "Active Picks",    "value": f"{picks_count} picks ✅" if picks_count else "None yet ⏳", "inline": True},
            ],
            "footer": {"text": f"Health check · {time_str}"},
        }

        from src.workers.alert_worker import _run_async
        _run_async(_post({"embeds": [embed]}))
        logger.info("Health check posted at %s", time_str)
        return {"status": "ok", "props": len(odds_props) + len(kalshi_props)}

    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {"status": "error", "error": str(e)}


def yesterday_recap():
    """6 AM ET — yesterday's slip results from Redis + today's game count."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    from datetime import datetime, timedelta
    import zoneinfo, json

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # Pull results from Redis slip tracker (not DB — DB is unused)
    # days_back=2 so morning recap at 6 AM catches slips from yesterday (created the day before)
    wins, losses, pushes, winner_slips, loser_slips = _load_slip_results(days_back=1)
    total = wins + losses

    if total == 0:
        record_str = "No settled picks yesterday — results appear as games finish."
        color = 0x607D8B
    else:
        pct = round(wins / total * 100) if total else 0
        record_str = f"**{wins}W – {losses}L** ({pct}% hit rate)"
        color = 0x00C851 if wins >= losses else 0xE53935

    win_text  = "\n".join(_slip_summary_line(s) for s in winner_slips[:3]) or "—"
    loss_text = "\n".join(_slip_summary_line(s) for s in loser_slips[:3])  or "—"

    # Sport breakdown from winner/loser slips
    sport_stats: dict = {}
    for slip in winner_slips + loser_slips:
        for pick in slip.get("picks", []):
            sk = pick.get("sport_key") or pick.get("sport") or ("kalshi" if pick.get("question") else "other")
            if sk not in sport_stats:
                sport_stats[sk] = {"w": 0, "l": 0}
            if slip.get("status") == "cashed":
                sport_stats[sk]["w"] += 1
            else:
                sport_stats[sk]["l"] += 1
    sport_lines = [f"{sk.split('_')[-1].upper()}: {v['w']}W-{v['l']}L" for sk, v in sport_stats.items()]
    sport_breakdown = "  ".join(sport_lines) or "—"

    # Today's games — read from Sofascore Redis cache (written by scan_todays_games at 8 AM)
    # Falls back to Odds API snapshot game list if cache is empty
    today_games_text = "—"
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        day_raw   = r.get("sofascore:day_games")
        night_raw = r.get("sofascore:night_games")
        all_events = []
        for raw in (day_raw, night_raw):
            if raw:
                try:
                    all_events.extend(json.loads(raw))
                except Exception:
                    pass

        _SPORT_LABELS = {
            "basketball_nba":            "🏀 NBA",
            "americanfootball_nfl":      "🏈 NFL",
            "baseball_mlb":              "⚾ MLB",
            "icehockey_nhl":             "🏒 NHL",
            "soccer_epl":                "⚽ EPL",
            "soccer_uefa_champs_league": "⚽ UCL",
            "soccer_usa_mls":            "⚽ MLS",
            "soccer_fifa_world_cup":     "⚽ World Cup",
            "basketball_ncaab":          "🏀 NCAAB",
            "americanfootball_ncaaf":    "🏈 NCAAF",
            "mma_mixed_martial_arts":    "🥊 MMA",
            "tennis_atp_french_open":    "🎾 French Open",
            "tennis_wta_french_open":    "🎾 French Open",
        }

        if all_events:
            lines = []
            for ev in all_events[:15]:
                home  = ev.get("home_team", "")
                away  = ev.get("away_team", "")
                sport = ev.get("sport_key", "")
                label = _SPORT_LABELS.get(sport, sport.split("_")[-1].upper() if sport else "")
                ct    = ev.get("commence_time", "")
                time_fmt = ""
                if ct:
                    try:
                        from dateutil.parser import parse as _p
                        time_fmt = " · " + _p(ct).astimezone(zoneinfo.ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
                    except Exception:
                        pass
                if home and away:
                    lines.append(f"• {label}: **{away} @ {home}**{time_fmt}")
            today_games_text = "\n".join(lines) if lines else "*No games cached yet — updates after 8 AM scan.*"
            if len(all_events) > 15:
                today_games_text += f"\n*… and {len(all_events) - 15} more*"
        else:
            today_games_text = "*No games cached yet — updates after 8 AM scan.*"
    except Exception as e:
        logger.warning("yesterday_recap: today's games fetch failed: %s", e)
        today_games_text = "—"

    import hashlib
    date_str = et.strftime("%b %-d, %Y")
    slip_id  = hashlib.md5(f"recap{date_str}".encode()).hexdigest()[:8].upper()

    is_monday = et.weekday() == 0
    week_num  = ((et.date() - _BOT_LAUNCH).days // 7) + 1
    title_str = (
        f"🟢  NEW WEEK · WEEK {week_num}  ·  {et.strftime('%A, %b %-d')}"
        if is_monday else
        f"📊  MORNING RECAP  ·  {et.strftime('%A, %b %-d')}"
    )
    embed = {
        "title": title_str,
        "description": (
            f"**{date_str}**  ·  Slip `#{slip_id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        "color": color,
        "fields": [
            {"name": "YESTERDAY'S RECORD", "value": record_str,    "inline": True},
            {"name": "BY SPORT",           "value": sport_breakdown or "—", "inline": True},
            {"name": "​",             "value": "​",         "inline": True},
            {"name": "✅  WINNERS",         "value": win_text,     "inline": True},
            {"name": "❌  LOSERS",          "value": loss_text,    "inline": True},
            {"name": "​",             "value": "​",         "inline": True},
            {"name": "📅  TODAY'S GAMES",   "value": today_games_text, "inline": False},
            {
                "name":  "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "value": "☀️  Day entry  **10:30 AM ET**  ·  🌙  Night entry  **4:30 PM ET**",
                "inline": False,
            },
        ],
        "footer": {"text": f"6:00 AM ET  ·  {date_str}  ·  Day entry at 10:30 AM ET"},
    }

    try:
        from src.workers.alert_worker import _run_async
        _run_async(_post({"embeds": [embed]}))
        logger.info("Yesterday recap sent: %dW-%dL-%dP", wins, losses, pushes)
    except Exception as e:
        logger.error("yesterday_recap post failed: %s", e)

    return {"wins": wins, "losses": losses, "pushes": pushes, "total": total}
