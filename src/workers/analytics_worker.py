"""Analytics worker — daily/weekly summaries, self-improvement, portfolio snapshots."""
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)

# ── Sleep window: 3 AM – 5 AM Eastern ─────────────────────────────────────────

def is_sleep_time() -> bool:
    """Return True if current Eastern time is inside the 3 AM–5 AM sleep window."""
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


@app.task
def send_daily_summary():
    from src.engines.summary_engine import get_daily_summary
    from src.engines.ai_engine import write_daily_summary
    from src.engines.clv_engine import aggregate_clv

    daily = get_daily_summary()
    clv_stats = aggregate_clv("daily")

    # Fetch actual picks and results from DB for the summary
    from src.db.session import get_db
    from src.db.models import Pick
    from datetime import datetime, timedelta
    with get_db() as db:
        today_picks = db.query(
            Pick.selection, Pick.sport, Pick.american_odds_at_gen,
            Pick.ev_pct, Pick.units, Pick.recommendation,
        ).filter(
            Pick.generated_at >= datetime.utcnow() - timedelta(hours=24),
            Pick.recommendation == "BET",
        ).limit(20).all()
        settled = db.query(
            Pick.selection, Pick.sport, Pick.result, Pick.actual_pnl_units,
        ).filter(
            Pick.settled_at >= datetime.utcnow() - timedelta(hours=24),
            Pick.result.isnot(None),
        ).limit(20).all()

    picks_dicts = [
        {"bet": sel, "sport": sp, "odds": odds, "ev": ev, "units": u}
        for sel, sp, odds, ev, u, _ in today_picks
    ]
    results_dicts = [
        {"bet": sel, "sport": sp, "result": res, "pnl": pnl}
        for sel, sp, res, pnl in settled
    ]
    summary = write_daily_summary(picks_dicts, results_dicts, clv_stats, bankroll=daily.get("net_units", 0))

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_daily_summary
    _run_async(post_daily_summary(summary))

    logger.info("Daily summary sent")
    return {"summary_length": len(summary), "stats": daily}


@app.task
def send_weekly_summary():
    """Fires Sunday midnight Eastern — full week recap including prop W/L ratio."""
    from src.engines.summary_engine import get_weekly_summary
    from src.engines.ai_engine import write_weekly_summary

    weekly = get_weekly_summary()
    props  = weekly.get("props", {})

    # Build prop section to append to the AI summary
    prop_section = ""
    if props.get("total", 0) > 0:
        hit  = props["hit_rate"] * 100
        record = f"{props['wins']}W - {props['losses']}L"
        if props.get("pushes"):
            record += f" - {props['pushes']}P"
        prop_section = (
            f"\n\n**PrizePicks Props:** {record} ({hit:.1f}% hit rate)"
        )
        if props.get("best_sport"):
            sport = props["best_sport"].split("_")[-1].upper()
            prop_section += f" | Best sport: {sport}"
        if props.get("best_stat"):
            prop_section += f" | Best stat: {props['best_stat']}"

    summary = write_weekly_summary(weekly) + prop_section

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import post_weekly_summary
    _run_async(post_weekly_summary(summary))

    logger.info("Weekly summary sent")
    return {"summary_length": len(summary), "stats": weekly}


@app.task
def send_weekly_fresh_start():
    """Fires Monday 12:05 AM Eastern — signals start of new betting week."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from datetime import datetime

    week_num = datetime.now().isocalendar()[1]
    embed = _embed(
        title="🟢 New Week — Fresh Start",
        description=(
            f"**Week {week_num}** is now live.\n\n"
            "Props scanning restarts. Picks engine is active.\n"
            "All sports monitored 24/7. Good luck this week! 🎯"
        ),
        color=0x1565C0,
    )
    _run_async(_post({"embeds": [embed]}))
    logger.info("Weekly fresh-start alert sent")
    return {"week": week_num}


@app.task
def send_monthly_summary():
    from src.engines.summary_engine import get_monthly_summary
    from datetime import datetime

    now = datetime.utcnow()
    monthly = get_monthly_summary(year=now.year, month=now.month)

    lines = [
        f"**Monthly Summary — {now.strftime('%B %Y')}**",
        f"Total Bets: {monthly['total_bets']} | P&L: {monthly['total_profit']:+.2f}u | ROI: {monthly['total_roi']:.1%}",
        f"Best Sport: {monthly['best_sport'] or '—'} | Best Market: {monthly['best_market'] or '—'}",
        f"Avg CLV: {monthly['avg_clv']:.2%}",
        f"Win Streak: {monthly['largest_winning_streak']} | Loss Streak: {monthly['largest_losing_streak']}",
    ]
    summary = "\n".join(lines)

    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import send_to_channel
    _run_async(send_to_channel("monthly-summary", content=summary))

    logger.info("Monthly summary sent")
    return {"summary_length": len(summary), "stats": monthly}


@app.task
def enter_sleep_mode():
    """3 AM Eastern — pause scanning, post goodnight message with W/L/P summary, run self-improvement."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from src.engines.self_improvement_engine import run_full_self_improvement
    from src.db.session import get_db
    from src.db.models import PropResult
    from datetime import datetime, timedelta
    import zoneinfo

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    today = et.date()

    # Pull today's settled results (games that finished today before 3 AM)
    try:
        with get_db() as db:
            rows = db.query(PropResult).filter(
                PropResult.settled_at >= datetime.combine(today, datetime.min.time()),
                PropResult.settled_at <  datetime.combine(today + timedelta(days=1), datetime.min.time()),
            ).all()
    except Exception as e:
        logger.warning("enter_sleep_mode: DB query failed: %s", e)
        rows = []

    total  = len(rows)
    wins   = sum(1 for r in rows if r.result == "won")
    losses = sum(1 for r in rows if r.result == "lost")
    pushes = sum(1 for r in rows if r.result == "push")

    if total == 0:
        record_str = "No settled picks today — results appear as games finish."
        color = 0x1A237E
    else:
        pct = round(wins / total * 100) if total else 0
        record_str = f"**{wins}W – {losses}L – {pushes}P** ({pct}% hit rate)"
        color = 0x1A237E

    # Top winners to list
    winners = [r for r in rows if r.result == "won"][:5]

    def _row(r):
        return f"• **{r.subject}** {r.stat} {r.line} ({r.direction.upper()})"

    win_text = "\n".join(_row(r) for r in winners) or "—"

    # Run self-improvement silently while sleeping
    try:
        run_full_self_improvement()
        logger.info("Self-improvement ran during sleep window")
    except Exception as e:
        logger.warning("Self-improvement failed during sleep: %s", e)

    fields = [
        {"name": "Today's Record", "value": record_str, "inline": False},
    ]
    if winners:
        fields.append({"name": "✅ Top Winners", "value": win_text, "inline": False})

    embed = _embed(
        title="🌙 Goodnight — See you at 5 AM",
        description=(
            f"Scanning paused until **5:00 AM ET**.\n"
            "Self-improvement cycle running on tonight's results."
        ),
        color=color,
        fields=fields,
    )
    _run_async(_post({"embeds": [embed]}))
    logger.info("Sleep mode entered at %s ET", et.strftime("%H:%M"))
    return {"sleep_entered": et.isoformat(), "wins": wins, "losses": losses, "pushes": pushes}


@app.task
def wake_up_brief():
    """5 AM Eastern — health check + platform status card."""
    import time
    import httpx
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    from src.core.config import (
        DISCORD_WEBHOOK_URL, OPENAI_API_KEY, ODDS_API_KEY, REDIS_URL,
    )
    from datetime import datetime
    import zoneinfo

    et_now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # ── Real health checks for every service we use ─────────────────────────────
    def _ping(label: str, fn) -> str:
        try:
            t0  = time.monotonic()
            ok  = fn()
            ms  = int((time.monotonic() - t0) * 1000)
            if ok is False:
                return f"❌  {label:<16} —  unhealthy"
            return f"✅  {label:<16} —  {ms}ms"
        except Exception as exc:
            short = str(exc)[:40]
            return f"❌  {label:<16} —  {short}"

    def _check_prizepicks():
        r = httpx.get("https://api.prizepicks.com/projections?per_page=1", timeout=6,
                      headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500

    def _check_underdog():
        r = httpx.get("https://api.underdogfantasy.com/beta/v5/over_under_lines", timeout=6,
                      headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500

    def _check_odds_api():
        r = httpx.get(f"https://api.the-odds-api.com/v4/sports?apiKey={ODDS_API_KEY}", timeout=6)
        return r.status_code == 200

    def _check_kalshi():
        r = httpx.get("https://external-api.kalshi.com/trade-api/v2/exchange/status", timeout=6)
        data = r.json()
        return data.get("exchange_active", False) is not False

    def _check_polymarket():
        r = httpx.get("https://gamma-api.polymarket.com/markets?limit=1", timeout=6)
        return r.status_code == 200

    def _check_discord():
        if not DISCORD_WEBHOOK_URL:
            return False
        r = httpx.get(DISCORD_WEBHOOK_URL, timeout=6)
        return r.status_code in (200, 405)   # 405 = method not allowed but webhook is alive

    def _check_openai():
        import openai
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        models = client.models.list()
        return len(list(models)) > 0

    def _check_redis():
        import redis as _r
        return _r.from_url(REDIS_URL, socket_connect_timeout=3).ping()

    service_checks = [
        ("PrizePicks",  _check_prizepicks),
        ("Underdog",    _check_underdog),
        ("Odds API",    _check_odds_api),
        ("Kalshi",      _check_kalshi),
        ("Polymarket",  _check_polymarket),
        ("Discord",     _check_discord),
        ("OpenAI",      _check_openai),
        ("Redis",       _check_redis),
    ]

    results = [_ping(label, fn) for label, fn in service_checks]
    health_block = "\n".join(f"`{line}`" for line in results)
    all_healthy  = all(line.startswith("✅") for line in results)
    health_title = "🔧 Service Health — All Systems Go ✅" if all_healthy else "🔧 Service Health — ⚠️ Issues Detected"

    # ── Active game count ───────────────────────────────────────────────────────
    games_today = "—"
    try:
        import json, redis as _r
        red = _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = red.get("props:active_picks")
        if raw:
            picks = json.loads(raw)
            games_today = str(len({p.get("team", "") for p in picks if p.get("team")}))
    except Exception:
        pass

    fields = [
        {"name": "Trading Mode",       "value": "**LIVE** 🟢",                                   "inline": True},
        {"name": "Active Games Today", "value": games_today,                                      "inline": True},
        {"name": "Platforms",
         "value": "🏆 PrizePicks · 🐶 Underdog · 🪨 HardRock · 📈 Kalshi · 🟣 Polymarket",
         "inline": False},
        {"name": health_title,         "value": health_block,                                     "inline": False},
        {"name": "Next Post",          "value": "**6:00 AM ET** — Good Morning Summary",         "inline": True},
        {"name": "Scan Intervals",
         "value": "Props every 5 min · News every 15 min · Picks every 10 min",
         "inline": False},
    ]

    embed = {
        "title": f"🚀 Bot Online — {et_now.strftime('%-I:%M %p ET')}",
        "description": (
            "Bot is online and scanning markets on "
            "🏆 PrizePicks + 🐶 Underdog + 🪨 HardRock + 📈 Kalshi + 🟣 Polymarket.\n"
            "ALERT ALERT fires automatically on any line move or prop removal."
        ),
        "color": 0x00C851,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", False)}
            for f in fields
        ],
        "footer": {"text": f"Sports Intelligence Platform · {et_now.strftime('%A, %B %-d')}"},
    }
    _run_async(_post({"embeds": [embed]}))
    logger.info("Wake-up brief sent (5 AM) — healthy=%s", all_healthy)
    return {"woke_up": True, "all_healthy": all_healthy}


@app.task
def todays_recap():
    """
    2:59 AM Eastern — full Today's RECAP before sleep mode kicks in at 3 AM.
    Shows every pick made today, all settled results, W/L/P record, ROI, best/worst pick.
    """
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    from src.db.session import get_db
    from src.db.models import PropResult
    from datetime import datetime, timedelta, date
    import zoneinfo, json

    et_tz  = zoneinfo.ZoneInfo("America/New_York")
    et_now = datetime.now(et_tz)
    today  = et_now.date()

    # ── Pull all prop results settled today ─────────────────────────────────────
    try:
        with get_db() as db:
            rows = db.query(PropResult).filter(
                PropResult.settled_at >= datetime.combine(today, datetime.min.time()),
                PropResult.settled_at <  datetime.combine(today + timedelta(days=1), datetime.min.time()),
            ).order_by(PropResult.settled_at.desc()).all()
    except Exception as e:
        logger.warning("todays_recap: DB query failed: %s", e)
        rows = []

    total  = len(rows)
    wins   = sum(1 for r in rows if r.result == "won")
    losses = sum(1 for r in rows if r.result == "lost")
    pushes = sum(1 for r in rows if r.result == "push")

    # ── Also pull active (unsettled) picks from Redis ───────────────────────────
    active_count = 0
    active_lines: list[str] = []
    try:
        from src.core.config import REDIS_URL
        import redis as _r
        red = _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = red.get("props:active_picks")
        if raw:
            active_picks = json.loads(raw)
            active_count = len(active_picks)
            for p in active_picks[:5]:
                arrow = "⬆️" if (p.get("direction") or "").lower() == "over" else "⬇️"
                active_lines.append(
                    f"• **{p.get('subject')}** {p.get('stat')} {p.get('line')} {arrow}"
                )
    except Exception:
        pass

    # ── Build result lines ──────────────────────────────────────────────────────
    result_lines: list[str] = []
    best_pick = None
    worst_pick = None
    for r in rows:
        icon = "✅" if r.result == "won" else "❌" if r.result == "lost" else "➖"
        result_lines.append(
            f"{icon} **{r.subject}** {r.stat} {r.line} {r.direction.upper()} → {r.actual_value}"
        )
        if r.result == "won" and best_pick is None:
            best_pick = r
        if r.result == "lost" and worst_pick is None:
            worst_pick = r

    # ── Stats ───────────────────────────────────────────────────────────────────
    if total > 0:
        hit_rate = round(wins / total * 100)
        roi      = round((wins - losses) / total * 100, 1)
        record_str = f"**{wins}W — {losses}L — {pushes}P**  ·  {hit_rate}% hit rate  ·  ROI: {roi:+.1f}%"
        color = 0x00C851 if wins >= losses else 0xFF1744
    else:
        record_str = "No settled picks today."
        color = 0x546E7A

    fields: list[dict] = [
        {"name": "📊 Today's Record", "value": record_str, "inline": False},
    ]

    if result_lines:
        fields.append({
            "name": f"🗂️ All Picks ({total} settled)",
            "value": "\n".join(result_lines[:15]) or "—",
            "inline": False,
        })

    if best_pick:
        fields.append({
            "name": "🏆 Best Pick",
            "value": f"**{best_pick.subject}** {best_pick.stat} {best_pick.line} {best_pick.direction.upper()} → {best_pick.actual_value} ✅",
            "inline": True,
        })
    if worst_pick:
        fields.append({
            "name": "💀 Worst Pick",
            "value": f"**{worst_pick.subject}** {worst_pick.stat} {worst_pick.line} {worst_pick.direction.upper()} → {worst_pick.actual_value} ❌",
            "inline": True,
        })

    if active_lines:
        fields.append({
            "name": f"⏳ Still Active ({active_count} picks — results pending)",
            "value": "\n".join(active_lines),
            "inline": False,
        })

    fields.append({
        "name": "🌙 Going to Sleep",
        "value": "Scanning paused until **5:00 AM ET**. Self-improvement running now.",
        "inline": False,
    })

    embed = {
        "title": f"📋 Today's RECAP — {et_now.strftime('%A, %B %-d')}",
        "color": color,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", False)}
            for f in fields
        ],
        "footer": {"text": f"Sports Intelligence Platform · See you at 5 AM ET"},
    }
    _run_async(_post({"embeds": [embed]}))
    logger.info("Today's RECAP sent: %dW %dL %dP", wins, losses, pushes)
    return {"wins": wins, "losses": losses, "pushes": pushes, "active": active_count}


@app.task
def run_self_improvement():
    from src.engines.self_improvement_engine import run_full_self_improvement
    result = run_full_self_improvement()
    logger.info("Self-improvement cycle done: %s periods", len(result))
    return result


@app.task
def snapshot_portfolio():
    from src.engines.portfolio_engine import get_performance_stats
    from src.db.session import get_db
    from src.db.models import BankrollSnapshot
    from datetime import datetime

    stats = get_performance_stats("lifetime")
    daily_stats = get_performance_stats("daily")
    with get_db() as db:
        db.add(BankrollSnapshot(
            recorded_at  = datetime.utcnow(),
            balance      = stats.get("net_units", 0),
            units_total  = daily_stats.get("net_units", 0),
            note         = "auto",
        ))
    logger.info("Portfolio snapshot saved")
    return stats


@app.task
def cleanup_old_snapshots():
    """Delete OddsSnapshots older than 7 days — prevents unbounded table growth.

    Without cleanup: ~6.9M rows/day accumulate and eventually kill query performance.
    """
    from src.db.session import get_db
    from src.db.models import OddsSnapshot, LineMovement, AlertRecord
    from datetime import datetime, timedelta

    cutoff_snapshots = datetime.utcnow() - timedelta(days=7)
    cutoff_alerts    = datetime.utcnow() - timedelta(days=30)

    with get_db() as db:
        deleted_snaps = db.query(OddsSnapshot).filter(
            OddsSnapshot.captured_at < cutoff_snapshots
        ).delete(synchronize_session=False)

        deleted_alerts = db.query(AlertRecord).filter(
            AlertRecord.sent_at < cutoff_alerts
        ).delete(synchronize_session=False)

    logger.info(
        "Cleanup: deleted %d old snapshots, %d old alert records",
        deleted_snaps, deleted_alerts,
    )
    return {"snapshots_deleted": deleted_snaps, "alerts_deleted": deleted_alerts}


@app.task
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
        all_raw = r.get("props:all")
        props = json.loads(all_raw) if all_raw else []
        pp_count  = sum(1 for p in props if p.get("source") == "prizepicks")
        ud_count  = sum(1 for p in props if p.get("source") == "underdog")
        last_hash = r.get("props:last_picks_hash")
        active_picks_raw = r.get("props:active_picks")
        active_picks = json.loads(active_picks_raw) if active_picks_raw else []
        picks_count = len(active_picks)

        et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        time_str = et.strftime("%I:%M %p ET")

        embed = {
            "title": "🟢 Bot Online",
            "description": (
                f"Scanning live props every 5 minutes.\n"
                f"Top picks posted when new high-confidence bets are found."
            ),
            "color": 0x00C851,
            "fields": [
                {"name": "PrizePicks",  "value": f"{pp_count:,} props", "inline": True},
                {"name": "Underdog",    "value": f"{ud_count:,} props", "inline": True},
                {"name": "Active Picks", "value": f"{picks_count} picks ✅" if picks_count else "None yet ⏳", "inline": True},
            ],
            "footer": {"text": f"Health check · {time_str}"},
        }

        import asyncio
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Health check posted at %s", time_str)
        return {"status": "ok", "props": len(props)}

    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.task
def yesterday_recap():
    """6 AM ET — yesterday's pick results + today's game count from DB."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    from src.db.session import get_db
    from src.db.models import PropResult
    from datetime import datetime, timedelta
    import zoneinfo, json

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    yesterday = (et - timedelta(days=1)).date()

    try:
        with get_db() as db:
            rows = db.query(PropResult).filter(
                PropResult.settled_at >= datetime.combine(yesterday, datetime.min.time()),
                PropResult.settled_at <  datetime.combine(et.date(),  datetime.min.time()),
            ).all()
    except Exception as e:
        logger.warning("yesterday_recap: DB query failed: %s", e)
        rows = []

    total  = len(rows)
    wins   = sum(1 for r in rows if r.result == "won")
    losses = sum(1 for r in rows if r.result == "lost")
    pushes = sum(1 for r in rows if r.result == "push")

    if total == 0:
        record_str = "No settled picks yesterday — results appear as games finish."
        color = 0x607D8B
    else:
        pct = round(wins / total * 100) if total else 0
        record_str = f"**{wins}W – {losses}L – {pushes}P** ({pct}% hit rate)"
        color = 0x00C851 if wins >= losses else 0xE53935

    # Best picks yesterday
    winners = [r for r in rows if r.result == "won"][:3]
    losers  = [r for r in rows if r.result == "lost"][:3]

    def _row(r):
        return f"• **{r.subject}** {r.stat} {r.line} ({r.direction.upper()})"

    win_text  = "\n".join(_row(r) for r in winners) or "—"
    loss_text = "\n".join(_row(r) for r in losers)  or "—"

    # Sport breakdown
    sport_stats: dict = {}
    for r in rows:
        sk = r.sport_key or "other"
        if sk not in sport_stats:
            sport_stats[sk] = {"w": 0, "l": 0}
        if r.result == "won":
            sport_stats[sk]["w"] += 1
        elif r.result == "lost":
            sport_stats[sk]["l"] += 1
    sport_lines = [
        f"{sk.split('_')[-1].upper()}: {v['w']}W-{v['l']}L"
        for sk, v in sport_stats.items()
    ]
    sport_breakdown = "  ".join(sport_lines) or "—"

    # Today's games (active sports only, no off-season)
    today_games_text = "—"
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        from src.apis.espn import fetch_scoreboard, SPORT_MAP

        _OFF_SEASON: dict[str, list[int]] = {
            "americanfootball_nfl":           [3, 4, 5, 6, 7],
            "americanfootball_ncaaf":         [1, 2, 3, 4, 5, 6, 7, 8],
            "icehockey_nhl":                  [7, 8, 9],
            "basketball_ncaab":               [4, 5, 6, 7, 8, 9, 10, 11],
            "golf_masters_tournament_winner": [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12],
        }
        _SPORT_LABELS = {
            "basketball_nba":                 "🏀 NBA",
            "americanfootball_nfl":           "🏈 NFL",
            "baseball_mlb":                   "⚾ MLB",
            "icehockey_nhl":                  "🏒 NHL",
            "soccer_epl":                     "⚽ EPL",
            "soccer_uefa_champs_league":      "⚽ UCL",
            "soccer_usa_mls":                 "⚽ MLS",
            "soccer_fifa_world_cup":          "⚽ World Cup",
            "basketball_ncaab":               "🏀 NCAAB",
            "americanfootball_ncaaf":         "🏈 NCAAF",
            "mma":                            "🥊 MMA",
            "mma_mixed_martial_arts":         "🥊 MMA",
            "tennis":                         "🎾 Tennis",
            "tennis_atp_french_open":         "🎾 French Open",
            "golf_masters_tournament_winner": "⛳ Golf",
        }
        current_month = et.month
        active_keys = [sk for sk in SPORT_MAP if current_month not in _OFF_SEASON.get(sk, [])]
        all_today: list[str] = []
        with ThreadPoolExecutor(max_workers=12) as _pool:
            _futs = {_pool.submit(fetch_scoreboard, sk): sk for sk in active_keys}
            for _fut in _as_completed(_futs, timeout=20):
                sk = _futs[_fut]
                try:
                    games = _fut.result() or []
                    active = [g for g in games if not g.get("completed")]
                    label = _SPORT_LABELS.get(sk, sk)
                    for g in active[:3]:
                        home = g.get("home_team", "")
                        away = g.get("away_team", "")
                        if home and away:
                            all_today.append(f"{label}: {away} @ {home}")
                except Exception:
                    pass
        if all_today:
            today_games_text = "\n".join(f"• {g}" for g in sorted(all_today)[:15])
            if len(all_today) > 15:
                today_games_text += f"\n*… and {len(all_today) - 15} more*"
        else:
            today_games_text = "*No games scheduled yet — check back later.*"
    except Exception as e:
        logger.warning("yesterday_recap: today's games fetch failed: %s", e)
        today_games_text = "—"

    embed = {
        "title": f"📊 Yesterday's Results — {yesterday.strftime('%A, %B %-d')}",
        "description": record_str,
        "color": color,
        "fields": [
            {"name": "✅ Winners",        "value": win_text,        "inline": True},
            {"name": "❌ Losers",         "value": loss_text,       "inline": True},
            {"name": "By Sport",          "value": sport_breakdown, "inline": False},
            {"name": "📅 Today's Games",  "value": today_games_text, "inline": False},
            {"name": "Next Picks",        "value": "Props scanning every 5 min — posted when found", "inline": False},
        ],
        "footer": {"text": f"6:00 AM ET recap · {et.strftime('%B %-d, %Y')}"},
    }

    try:
        import asyncio
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Yesterday recap sent: %dW-%dL-%dP", wins, losses, pushes)
    except Exception as e:
        logger.error("yesterday_recap post failed: %s", e)

    return {"wins": wins, "losses": losses, "pushes": pushes, "total": total}
