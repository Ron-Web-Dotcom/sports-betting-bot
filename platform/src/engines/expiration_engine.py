"""Pick expiration engine — assess urgency of placing a bet based on line movement."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


@dataclass
class ExpirationWindow:
    current_odds: int
    expected_odds: int
    bet_before: str  # ISO timestamp
    urgency: Literal["bet_now", "value_expiring", "wait", "better_line_expected"]
    minutes_remaining: float
    reasoning: str


def assess_expiration(pick_id: int, current_odds: int, line_movements: list[dict]) -> ExpirationWindow:
    from src.db.session import get_db
    from src.db.models import Pick, Game

    now = datetime.now(timezone.utc)
    minutes_to_game = float("inf")

    with get_db() as db:
        pick = db.query(Pick).filter(Pick.id == pick_id).first()
        if pick:
            game = db.query(Game).filter(Game.id == pick.game_id).first()
            if game and game.commence_time:
                ct = game.commence_time
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
                diff = (ct - now).total_seconds() / 60
                minutes_to_game = max(0.0, diff)

    # game is imminent — also treat unknown start time (inf) as bet_now
    if minutes_to_game < 60 or minutes_to_game == float("inf"):
        # Cap infinite time to 60 min window; timedelta(minutes=inf) raises OverflowError
        safe_minutes = min(minutes_to_game, 60) if minutes_to_game != float("inf") else 60
        return ExpirationWindow(
            current_odds=current_odds,
            expected_odds=current_odds,
            bet_before=(now + timedelta(minutes=safe_minutes)).isoformat(),
            urgency="bet_now",
            minutes_remaining=minutes_to_game,
            reasoning="Game starts in less than 60 minutes — bet now.",
        )

    if not line_movements:
        return ExpirationWindow(
            current_odds=current_odds,
            expected_odds=current_odds,
            bet_before=(now + timedelta(hours=4)).isoformat(),
            urgency="bet_now",
            minutes_remaining=minutes_to_game,
            reasoning="No line movement detected — value appears stable.",
        )

    # analyse last 3 moves
    recent = line_movements[-3:]
    deltas = []
    for mv in recent:
        before = mv.get("odds_before", 0)
        after = mv.get("odds_after", 0)
        deltas.append(after - before)

    moving_against = all(d > 0 for d in deltas) if deltas else False  # positive = odds getting worse (higher for fav)
    moving_in_favor = all(d < 0 for d in deltas) if deltas else False

    avg_move = sum(deltas) / len(deltas) if deltas else 0
    expected_odds = current_odds + int(avg_move)

    if moving_against:
        return ExpirationWindow(
            current_odds=current_odds,
            expected_odds=expected_odds,
            bet_before=(now + timedelta(minutes=30)).isoformat(),
            urgency="value_expiring",
            minutes_remaining=minutes_to_game,
            reasoning="Line moving against us — bet within 30 minutes before value disappears.",
        )

    if moving_in_favor:
        return ExpirationWindow(
            current_odds=current_odds,
            expected_odds=expected_odds,
            bet_before=(now + timedelta(hours=2)).isoformat(),
            urgency="better_line_expected",
            minutes_remaining=minutes_to_game,
            reasoning="Line moving in our favor — wait for a better number.",
        )

    return ExpirationWindow(
        current_odds=current_odds,
        expected_odds=current_odds,
        bet_before=(now + timedelta(hours=4)).isoformat(),
        urgency="bet_now",
        minutes_remaining=minutes_to_game,
        reasoning="Line is stable — value is available now.",
    )


def format_expiration_embed(window: ExpirationWindow) -> dict:
    urgency_labels = {
        "bet_now": "🟢 BET NOW",
        "value_expiring": "🔴 VALUE EXPIRING",
        "wait": "🟡 WAIT",
        "better_line_expected": "🔵 BETTER LINE EXPECTED",
    }
    return {
        "urgency": urgency_labels.get(window.urgency, window.urgency),
        "current_odds": f"{window.current_odds:+d}",
        "expected_odds": f"{window.expected_odds:+d}",
        "bet_before": window.bet_before,
        "minutes_remaining": f"{window.minutes_remaining:.0f} min",
        "reasoning": window.reasoning,
    }
