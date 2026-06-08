"""Best-time-to-bet advisor — predicts line direction and optimal bet timing."""
from dataclasses import dataclass
from typing import Literal


@dataclass
class BetTiming:
    recommendation: Literal["bet_now", "wait", "value_expiring", "better_line_expected"]
    confidence: float
    expected_line_direction: str
    reasoning: str


def advise_timing(
    event: dict,
    current_odds: int,
    historical_movements: list[dict],
    minutes_to_game: float,
) -> BetTiming:
    # < 15 min: always bet now
    if minutes_to_game < 15:
        return BetTiming(
            recommendation="bet_now",
            confidence=0.95,
            expected_line_direction="stable",
            reasoning="Game starts in less than 15 minutes — bet now regardless of line.",
        )

    if not historical_movements:
        return BetTiming(
            recommendation="bet_now",
            confidence=0.6,
            expected_line_direction="stable",
            reasoning="No historical movement data — value appears stable.",
        )

    # detect sharp money: sudden large move (>=10 pts in implied prob or >=20 american)
    last = historical_movements[-1]
    last_delta = abs((last.get("odds_after", 0) or 0) - (last.get("odds_before", 0) or 0))
    move_type = last.get("movement_type", "")
    if last_delta >= 20 or move_type in ("steam", "sharp"):
        return BetTiming(
            recommendation="bet_now",
            confidence=0.85,
            expected_line_direction="against",
            reasoning="Sharp/steam money detected — line will move further against us.",
        )

    # check last 3 moves for consistent direction
    recent = historical_movements[-3:]
    deltas = [(m.get("odds_after", 0) or 0) - (m.get("odds_before", 0) or 0) for m in recent]

    all_positive = all(d > 0 for d in deltas) if deltas else False
    all_negative = all(d < 0 for d in deltas) if deltas else False

    if all_positive:
        # line drifting away — public money pattern, wait for reverse line movement
        if move_type in ("public",) or last_delta < 10:
            return BetTiming(
                recommendation="wait",
                confidence=0.7,
                expected_line_direction="reverse",
                reasoning="Public money pushing line — wait for reverse line movement.",
            )
        return BetTiming(
            recommendation="value_expiring",
            confidence=0.75,
            expected_line_direction="against",
            reasoning="Line consistently moving against us — value expiring soon.",
        )

    if all_negative:
        return BetTiming(
            recommendation="better_line_expected",
            confidence=0.7,
            expected_line_direction="favorable",
            reasoning="Line moving in our favor — better number expected.",
        )

    return BetTiming(
        recommendation="bet_now",
        confidence=0.65,
        expected_line_direction="stable",
        reasoning="Mixed line movement — current value is available now.",
    )


# avg line movement by hour before game (historical patterns by sport/market)
_HISTORICAL_PATTERNS: dict[str, dict] = {
    "basketball_nba:spread": {"avg_move_per_hour": 0.5, "sharp_window_hours": 2},
    "americanfootball_nfl:spread": {"avg_move_per_hour": 0.3, "sharp_window_hours": 6},
    "baseball_mlb:moneyline": {"avg_move_per_hour": 2.0, "sharp_window_hours": 1},
    "icehockey_nhl:puck_line": {"avg_move_per_hour": 0.2, "sharp_window_hours": 3},
}


def get_historical_pattern(sport: str, market: str) -> dict:
    key = f"{sport}:{market}"
    return _HISTORICAL_PATTERNS.get(key, {"avg_move_per_hour": 0.5, "sharp_window_hours": 2})
