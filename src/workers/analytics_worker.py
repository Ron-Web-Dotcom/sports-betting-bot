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
    """3 AM Eastern — pause scanning, post goodnight message, run self-improvement."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from src.engines.self_improvement_engine import run_full_self_improvement
    from datetime import datetime
    import zoneinfo

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # Run self-improvement silently while sleeping
    try:
        run_full_self_improvement()
        logger.info("Self-improvement ran during sleep window")
    except Exception as e:
        logger.warning("Self-improvement failed during sleep: %s", e)

    embed = _embed(
        title="🌙 Sleep Mode — Scanning Paused",
        description=(
            f"**{et.strftime('%-I:%M %p ET')}** — Going into sleep mode.\n\n"
            "• All live scanning paused until **5:00 AM ET**\n"
            "• Settlement and cleanup running in background\n"
            "• Self-improvement cycle running on tonight's results\n\n"
            "See you at 5 AM 👋"
        ),
        color=0x1A237E,
    )
    _run_async(_post({"embeds": [embed]}))
    logger.info("Sleep mode entered at %s ET", et.strftime("%H:%M"))
    return {"sleep_entered": et.isoformat()}


@app.task
def wake_up_brief():
    """5 AM Eastern — wake up, scan today's games, post morning brief."""
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.apis.espn import fetch_scoreboard, SPORT_MAP
    from datetime import datetime
    import zoneinfo

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    _SPORT_LABELS = {
        "basketball_nba":                 "🏀 NBA",
        "americanfootball_nfl":           "🏈 NFL",
        "baseball_mlb":                   "⚾ MLB",
        "icehockey_nhl":                  "🏒 NHL",
        "soccer_epl":                     "⚽ Premier League",
        "soccer_uefa_champs_league":      "⚽ Champions League",
        "soccer_usa_mls":                 "⚽ MLS",
        "basketball_ncaab":               "🏀 NCAAB",
        "americanfootball_ncaaf":         "🏈 NCAAF",
        "mma":                            "🥊 UFC/MMA",
        "tennis":                         "🎾 Tennis",
        "golf_masters_tournament_winner": "⛳ Golf",
    }

    # Fetch all scoreboards in parallel
    all_games: list[str] = []
    sport_counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_scoreboard, sk): sk for sk in SPORT_MAP}
        for future in as_completed(futures, timeout=20):
            sk = futures[future]
            try:
                games = future.result() or []
                active = [g for g in games if not g.get("completed")]
                if active:
                    label = _SPORT_LABELS.get(sk, sk)
                    sport_counts[label] = len(active)
                    for g in active[:3]:  # top 3 per sport in the brief
                        all_games.append(
                            f"{label}: {g.get('away_team','?')} @ {g.get('home_team','?')}"
                        )
            except Exception:
                pass

    total = sum(sport_counts.values())
    if all_games:
        games_text = "\n".join(f"• {g}" for g in all_games[:20])
        if total > 20:
            games_text += f"\n*… and {total - 20} more*"
    else:
        games_text = "*No games found yet — check back after 8 AM when lines open.*"

    sports_on = ", ".join(
        f"{label} ({n})" for label, n in sorted(sport_counts.items(), key=lambda x: -x[1])
    ) or "None yet"

    embed = _embed(
        title=f"☀️ Good Morning — {et.strftime('%A, %B %-d')}",
        description=(
            f"**{total} games** on today across {len(sport_counts)} sports.\n\n"
            f"{games_text}\n\n"
            f"⏳ **Props lines open at 8 AM ET** — full pick recommendations coming then."
        ),
        color=0xF57F17,
        fields=[
            {"name": "Sports Active Today", "value": sports_on or "—", "inline": False},
            {"name": "Next Update", "value": "8:00 AM ET — Full props picks brief", "inline": False},
        ],
    )
    _run_async(_post({"embeds": [embed]}))
    logger.info("Wake-up brief sent: %d games across %d sports", total, len(sport_counts))
    return {"games_today": total, "sports": len(sport_counts)}


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
                {"name": "Last Picks",  "value": "Updated ✅" if last_hash else "Pending ⏳", "inline": True},
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
