"""Alert worker — routes all Discord notifications asynchronously."""
import asyncio
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run a coroutine from a sync Celery context.

    asyncio.run() always creates a fresh event loop and tears it down
    cleanly — safe to call from any Celery worker thread.
    """
    return asyncio.run(coro)


@app.task
def send_pick_alerts(picks: list[dict]):
    from src.discord_bot.bot import post_pick
    for pick in picks:
        try:
            _run_async(post_pick(pick))
        except Exception as e:
            logger.error("Failed to send pick alert: %s", e)


@app.task
def send_parlay_alerts(parlays: list[dict]):
    from src.discord_bot.bot import post_parlay
    for parlay in parlays:
        try:
            _run_async(post_parlay(parlay))
        except Exception as e:
            logger.error("Failed to send parlay alert: %s", e)


@app.task
def send_line_movement_alerts(movements: list[dict]):
    from src.discord_bot.bot import post_line_movement
    for mov in movements:
        try:
            _run_async(post_line_movement(mov))
        except Exception as e:
            logger.error("Failed to send line movement alert: %s", e)


@app.task
def send_lineup_alerts(alerts: list[dict]):
    """Fire Discord alerts for injury/lineup changes that affect active props."""
    from src.discord_bot.bot import _post
    try:
        embeds = [
            {
                "title":       a["title"][:256],
                "description": a["description"][:4096],
                "color":       a["color"],
                "fields": [
                    {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
                    for f in a.get("fields", [])[:25]
                ],
            }
            for a in alerts[:10]
        ]
        import asyncio
        asyncio.run(_post({"embeds": embeds}))
        logger.info("Lineup alerts sent: %d", len(alerts))
    except Exception as e:
        logger.error("Failed to send lineup alerts: %s", e)


@app.task
def send_pp_parlay_alert(picks: list[dict]):
    from src.discord_bot.bot import post_pp_parlay
    try:
        _run_async(post_pp_parlay(picks))
    except Exception as e:
        logger.error("Failed to send PP parlay alert: %s", e)


@app.task
def send_hardrock_parlay_alert(picks: list[dict]):
    from src.discord_bot.bot import post_hardrock_parlay
    try:
        _run_async(post_hardrock_parlay(picks))
    except Exception as e:
        logger.error("Failed to send HardRock parlay alert: %s", e)


@app.task
def send_prop_pick_alerts(picks: list[dict]):
    from src.discord_bot.bot import post_prop_pick
    for pick in picks:
        try:
            _run_async(post_prop_pick(pick))
        except Exception as e:
            logger.error("Failed to send prop pick alert: %s", e)


@app.task
def send_prop_summary(picks: list[dict]):
    """Post ONE summary embed with all top picks — no per-pick spam."""
    from src.discord_bot.bot import _post
    from datetime import datetime
    if not picks:
        return

    # Sport display maps
    _emoji = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "basketball_ncaab": "🏀", "americanfootball_ncaaf": "🏈",
        "soccer_fifa_world_cup": "⚽", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_uefa_champs_league": "⚽", "mma_mixed_martial_arts": "🥊", "mma": "🥊",
        "tennis_atp_french_open": "🎾", "tennis": "🎾",
        "golf_masters_tournament_winner": "⛳",
    }
    _sport_name = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "basketball_ncaab": "NCAAB", "americanfootball_ncaaf": "NCAAF",
        "soccer_fifa_world_cup": "World Cup", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "soccer_uefa_champs_league": "UCL", "mma_mixed_martial_arts": "UFC/MMA", "mma": "UFC/MMA",
        "tennis_atp_french_open": "French Open", "tennis": "Tennis",
        "golf_masters_tournament_winner": "Golf",
    }

    lines = []
    for p in picks:
        sport = p.get("sport_key", "")
        emoji = _emoji.get(sport, "🎯")
        sport_label = _sport_name.get(sport, sport.replace("_", " ").title())
        direction = p.get("direction", "").upper()
        arrow = "⬆️" if direction == "OVER" else "⬇️"
        conf = round(p.get("confidence", 0) * 100)
        ev = round(p.get("ev_pct", 0) * 100, 1)
        source = p.get("source", "").title()
        game_time = p.get("game_time", "")
        time_str = ""
        if game_time:
            try:
                from dateutil.parser import parse as _parse
                import zoneinfo as _zi
                t = _parse(game_time).astimezone(_zi.ZoneInfo("America/New_York"))
                time_str = f" · {t.strftime('%-I:%M %p ET')}"
            except Exception:
                pass
        lines.append(
            f"{emoji} **{p.get('subject')}** — {p.get('stat')} {p.get('line')} {arrow} {direction}\n"
            f"  `{conf}% conf | +{ev}% edge | {sport_label} · {source}{time_str}`"
        )

    import zoneinfo
    now_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
    embed = {
        "title": f"🎯 Top Prop Picks — {now_str}",
        "description": "\n\n".join(lines),
        "color": 0x00C851,
        "footer": {"text": f"{len(picks)} picks · PrizePicks & Underdog · Bet responsibly"},
    }
    try:
        import asyncio
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Prop summary posted: %d picks", len(picks))
    except Exception as e:
        logger.error("Failed to send prop summary: %s", e)


@app.task
def send_prop_result_alert(pick: dict, result: str, actual: float):
    from src.discord_bot.bot import post_prop_result
    try:
        _run_async(post_prop_result(pick, result, actual))
    except Exception as e:
        logger.error("Failed to send prop result alert: %s", e)


@app.task
def send_underdog_entry(picks: list[dict]):
    """Post Underdog Fantasy entry card — Underdog-specific props only."""
    from src.discord_bot.bot import _post
    import asyncio
    if not picks:
        return

    _SPORT_LABELS = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "basketball_ncaab": "NCAAB", "americanfootball_ncaaf": "NCAAF",
        "soccer_fifa_world_cup": "World Cup", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "mma_mixed_martial_arts": "UFC/MMA", "mma": "UFC/MMA",
        "tennis_atp_french_open": "Tennis", "tennis": "Tennis",
    }

    n = min(len(picks), 6)
    picks = picks[:n]
    # Underdog uses flex payout tiers similar to PP
    _UD_MULTIPLIERS = {2: 3, 3: 6, 4: 10, 5: 20, 6: 40}
    mult = _UD_MULTIPLIERS.get(n, 40)
    avg_conf = round(sum((p.get("confidence") or 0) for p in picks) / n * 100)

    leg_blocks = []
    for i, p in enumerate(picks, 1):
        arrow = "↑" if (p.get("direction") or "over").lower() == "over" else "↓"
        sport = _SPORT_LABELS.get(p.get("sport_key", ""), p.get("sport_key", "").split("_")[-1].upper())
        subject = p.get("subject", "?")
        stat = p.get("stat", "")
        line = p.get("line", "?")
        conf = round((p.get("confidence") or 0) * 100)
        ev = round((p.get("ev_pct") or 0) * 100, 1)
        factors = p.get("key_factors") or []
        reasoning = (p.get("reasoning") or "").strip()
        top_reason = factors[0] if factors else (reasoning.split(".")[0].strip() if reasoning else "—")
        leg_blocks.append(
            f"`{i}.` {arrow} **{subject}** — {line} {stat}  ·  {sport}  ·  {conf}% conf  ·  +{ev}% edge\n"
            f"     └ {top_reason}"
        )

    fields = [
        {"name": "Legs",       "value": str(n),         "inline": True},
        {"name": "Multiplier", "value": f"**{mult}x**", "inline": True},
        {"name": "Avg Conf",   "value": f"{avg_conf}%", "inline": True},
        {"name": "⚠️", "value": "Place manually on Underdog Fantasy. Max 6 picks.", "inline": False},
    ]
    embed = {
        "title": f"🐶 Underdog Entry — {n} Picks  ·  {mult}x Payout",
        "description": "\n".join(leg_blocks),
        "color": 0xE65100,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
            for f in fields
        ],
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Underdog entry posted: %d picks", n)
    except Exception as e:
        logger.error("Failed to send Underdog entry: %s", e)


@app.task
def send_kalshi_entry(markets: list[dict]):
    """Post Kalshi prediction market entry card."""
    from src.discord_bot.bot import _post
    import asyncio
    if not markets:
        return

    n = min(len(markets), 8)
    markets = markets[:n]

    lines = []
    for m in markets:
        ticker = m.get("ticker") or m.get("id") or "?"
        title_text = m.get("title") or m.get("question") or ticker
        yes_price = m.get("yes_price") or m.get("yes_bid") or m.get("price")
        no_price = m.get("no_price") or m.get("no_bid")
        if yes_price is not None:
            try:
                yes_pct = round(float(yes_price) * 100) if float(yes_price) <= 1 else int(yes_price)
            except Exception:
                yes_pct = yes_price
            price_str = f"YES {yes_pct}¢"
            if no_price is not None:
                try:
                    no_pct = round(float(no_price) * 100) if float(no_price) <= 1 else int(no_price)
                except Exception:
                    no_pct = no_price
                price_str += f"  /  NO {no_pct}¢"
        else:
            price_str = "—"
        lines.append(f"• **{title_text}**  ·  {price_str}")

    embed = {
        "title": f"📈 Kalshi Entry — {n} Markets",
        "description": "\n".join(lines),
        "color": 0x00ACC1,
        "fields": [
            {"name": "⚠️", "value": "Place manually on Kalshi. Prediction markets — not sports books.", "inline": False},
        ],
        "footer": {"text": "Kalshi · prediction market prices"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Kalshi entry posted: %d markets", n)
    except Exception as e:
        logger.error("Failed to send Kalshi entry: %s", e)


@app.task
def send_games_starting_soon(games: list[dict]):
    """Post ONE embed: all games starting in ~30 min."""
    from src.discord_bot.bot import _post
    import asyncio
    if not games:
        return

    lines = []
    for g in games:
        home = g.get("home_team", "?")
        away = g.get("away_team", "?")
        sport = g.get("sport_key", "").split("_")[-1].upper()
        mins = g.get("minutes_remaining")
        time_str = f"~{int(mins)} min" if mins is not None else ""
        lines.append(f"• **{away} @ {home}**  ·  {sport}" + (f"  ·  {time_str}" if time_str else ""))

    embed = {
        "title": "⚠️ Games Starting Soon",
        "description": "\n".join(lines),
        "color": 0xF57F17,
        "footer": {"text": "Last chance to place your bets"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Games-starting-soon alert sent: %d games", len(games))
    except Exception as e:
        logger.error("Failed to send games-starting-soon alert: %s", e)


@app.task
def send_games_started(games: list[dict]):
    """Post ONE embed: all games that just went live."""
    from src.discord_bot.bot import _post
    import asyncio
    if not games:
        return

    lines = []
    for g in games:
        home = g.get("home_team", "?")
        away = g.get("away_team", "?")
        sport = g.get("sport_key", "").split("_")[-1].upper()
        lines.append(f"• **{away} @ {home}**  ·  {sport}")

    embed = {
        "title": "🏀 Games Are Live Now",
        "description": "\n".join(lines),
        "color": 0x00C851,
        "footer": {"text": "Games are underway — no more pre-game bets"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Games-started alert sent: %d games", len(games))
    except Exception as e:
        logger.error("Failed to send games-started alert: %s", e)


@app.task
def send_prop_change_alerts(changes: list[dict]):
    # Disabled — floods Discord with thousands of line moves every scan
    logger.debug("send_prop_change_alerts suppressed (%d changes)", len(changes))


@app.task
def send_pick_line_update(changes: list[dict]):
    """Alert only when one of our recommended picks moves line or goes off-board."""
    from src.discord_bot.bot import _post
    import asyncio
    if not changes:
        return

    _type_emoji = {"moved": "📊", "removed": "❌"}
    lines = []
    for c in changes:
        emoji = _type_emoji.get(c.get("change_type"), "ℹ️")
        subject = c.get("subject", "")
        stat = c.get("stat", "")
        direction = c.get("our_direction", "").upper()
        if c.get("change_type") == "moved":
            lines.append(f"{emoji} **{subject}** {stat} — line moved {c.get('old_line')} → **{c.get('new_line')}** (we picked {direction})")
        elif c.get("change_type") == "removed":
            lines.append(f"{emoji} **{subject}** {stat} — **OFF THE BOARD** (we picked {direction})")

    if not lines:
        return

    embed = {
        "title": "⚠️ Pick Update",
        "description": "\n".join(lines),
        "color": 0xFF6F00,
        "footer": {"text": "Line movement on your active picks"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Pick line update sent: %d changes", len(changes))
    except Exception as e:
        logger.error("Failed to send pick line update: %s", e)


@app.task
def send_result_alert(pick: dict, result: str):
    from src.discord_bot.bot import post_result
    try:
        _run_async(post_result(pick, result))
    except Exception as e:
        logger.error("Failed to send result alert: %s", e)


@app.task
def send_pregame_alerts():
    """Check all upcoming games and fire grouped pre-game alerts (one embed per window)."""
    from src.engines.timing_engine import upcoming_games_by_window, should_fire_alert, minutes_to_game
    from src.db.session import get_db
    from src.db.models import Game, AlertRecord
    from datetime import datetime

    # Extract plain values inside session — avoids DetachedInstanceError after close
    with get_db() as db:
        rows = db.query(
            Game.id, Game.home_team, Game.away_team, Game.commence_time
        ).filter(Game.commence_time >= datetime.utcnow()).all()

    events = [
        {
            "id": str(gid),
            "sport_key": "",   # not on Game model directly — resolved via Sport FK if needed
            "home_team": home,
            "away_team": away,
            "commence_time": ct.isoformat() if ct else "",
        }
        for gid, home, away, ct in rows
    ]

    windowed = upcoming_games_by_window(events)

    for window, games in windowed.items():
        # Collect all games in this window that haven't been alerted yet
        games_to_alert = []
        for event in games:
            game_id = event["id"]
            if not should_fire_alert(game_id, window):
                continue
            mins = minutes_to_game(event["commence_time"])
            games_to_alert.append({**event, "minutes_remaining": mins})

        if not games_to_alert:
            continue

        # Group: ~30-min window → "starting soon"; ~0-min window → "started"
        if window <= 5:
            # Games that just started — post one grouped embed
            send_games_started.delay(games_to_alert)
        else:
            # Games starting in ~30 min — post one grouped embed
            send_games_starting_soon.delay(games_to_alert)

        # Record alert so we don't re-fire
        with get_db() as db:
            for event in games_to_alert:
                db.add(AlertRecord(
                    alert_type=f"pregame_{window}",
                    channel=f"game-alerts:{event['id']}",
                    priority="high" if window <= 15 else "medium",
                    sent_at=datetime.utcnow(),
                ))
