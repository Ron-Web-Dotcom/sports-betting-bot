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
def send_prop_pick_alerts(picks: list[dict]):
    from src.discord_bot.bot import post_prop_pick
    for pick in picks:
        try:
            _run_async(post_prop_pick(pick))
        except Exception as e:
            logger.error("Failed to send prop pick alert: %s", e)


@app.task
def send_prop_result_alert(pick: dict, result: str, actual: float):
    from src.discord_bot.bot import post_prop_result
    try:
        _run_async(post_prop_result(pick, result, actual))
    except Exception as e:
        logger.error("Failed to send prop result alert: %s", e)


@app.task
def send_prop_change_alerts(changes: list[dict]):
    from src.discord_bot.bot import post_prop_changes
    try:
        _run_async(post_prop_changes(changes))
    except Exception as e:
        logger.error("Failed to send prop change alerts: %s", e)


@app.task
def send_result_alert(pick: dict, result: str):
    from src.discord_bot.bot import post_result
    try:
        _run_async(post_result(pick, result))
    except Exception as e:
        logger.error("Failed to send result alert: %s", e)


@app.task
def send_pregame_alerts():
    """Check all upcoming games and fire timed pre-game alerts."""
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
    from src.discord_bot.bot import post_game_alert

    for window, games in windowed.items():
        for event in games:
            game_id = event["id"]
            if not should_fire_alert(game_id, window):
                continue

            mins = minutes_to_game(event["commence_time"])
            try:
                _run_async(post_game_alert(event, mins))
            except Exception as e:
                logger.error("Failed to send pregame alert: %s", e)
                continue

            with get_db() as db:
                db.add(AlertRecord(
                    alert_type=f"pregame_{window}",
                    channel=f"game-alerts:{game_id}",
                    priority="high" if window <= 15 else "medium",
                    sent_at=datetime.utcnow(),
                ))
