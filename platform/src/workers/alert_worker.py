"""Alert worker — routes all Discord notifications asynchronously."""
import asyncio
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run a coroutine from a sync Celery context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


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

    with get_db() as db:
        upcoming = db.query(Game).filter(Game.commence_time >= datetime.utcnow()).all()

    events = [
        {
            "id": str(g.id),
            "sport_key": g.sport_key,
            "home_team": g.home_team,
            "away_team": g.away_team,
            "commence_time": g.commence_time.isoformat(),
        }
        for g in upcoming
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
