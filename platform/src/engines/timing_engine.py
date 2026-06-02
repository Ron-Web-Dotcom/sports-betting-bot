"""
Engine 11 — Timing Engine.

Manages pre-game alert windows and determines when to fire each alert tier.
Tracks time-to-game for all upcoming events.
"""
import logging
from datetime import datetime, timedelta
from src.core.config import PREGAME_ALERT_WINDOWS

logger = logging.getLogger(__name__)


def minutes_to_game(commence_time_str: str) -> float:
    """Return minutes until game start. Negative = game already started."""
    try:
        if isinstance(commence_time_str, datetime):
            game_time = commence_time_str
        else:
            from dateutil import parser
            game_time = parser.parse(commence_time_str)
        # Strip timezone info so both sides are naive UTC — mixing aware and
        # naive datetimes raises TypeError in Python 3.
        if hasattr(game_time, "tzinfo") and game_time.tzinfo is not None:
            game_time = game_time.replace(tzinfo=None)
        delta = game_time - datetime.utcnow()
        return delta.total_seconds() / 60
    except Exception:
        return 9999.0


def get_alert_window(minutes_remaining: float) -> int | None:
    """
    Return which pre-game alert window we're in, or None.
    Windows: 60, 30, 15, 10, 5, 0 minutes before game.
    """
    for window in sorted(PREGAME_ALERT_WINDOWS, reverse=True):
        if minutes_remaining <= window + 2:   # 2-min tolerance
            return window
    return None


def should_fire_alert(game_id: str, window: int) -> bool:
    """Check if this alert window has already been fired for this game."""
    try:
        from src.db.session import get_db
        from src.db.models import AlertRecord
        with get_db() as db:
            existing = db.query(AlertRecord).filter(
                AlertRecord.alert_type == f"pregame_{window}",
                AlertRecord.channel.contains(str(game_id)),
            ).first()
            return existing is None
    except Exception:
        return True


def get_urgency_level(minutes_remaining: float) -> str:
    """Return alert urgency based on time to game."""
    if minutes_remaining <= 5:   return "critical"
    if minutes_remaining <= 15:  return "high"
    if minutes_remaining <= 60:  return "medium"
    return "low"


def upcoming_games_by_window(all_events: list[dict]) -> dict[int, list[dict]]:
    """
    Group upcoming games by their pre-game alert window.
    Returns {window_minutes: [game_dicts]}
    """
    result: dict[int, list[dict]] = {w: [] for w in PREGAME_ALERT_WINDOWS}
    for event in all_events:
        mins = minutes_to_game(event.get("commence_time", ""))
        if mins < 0 or mins > 70:  # only active windows
            continue
        window = get_alert_window(mins)
        if window is not None:
            result[window].append(event)
    return result
