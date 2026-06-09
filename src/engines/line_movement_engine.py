"""
Engine 2b — Line Movement Engine (part of Sportsbook Comparison).

Detects: steam moves, sharp money, reverse line movement,
public fade opportunities, value expiry.
Triggers alerts when threshold breached.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from src.engines.ev_engine import implied_prob
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

STEAM_THRESHOLD_PCT     = 0.04   # 4% prob shift in < 10 minutes = steam
SHARP_THRESHOLD_PCT     = 0.03   # 3% consistent move across multiple books
VALUE_EXPIRY_THRESHOLD  = 0.03   # If odds tighten by 3% → alert "bet now"


@dataclass
class LineMovementAlert:
    game_id:      int | str
    event_name:   str
    market:       str
    selection:    str
    book:         str
    odds_before:  int
    odds_after:   int
    delta_pct:    float
    movement_type:str   # steam|sharp|reverse|public_fade|value_expiring
    detected_at:  datetime


def detect_movements(
    game_id: int | str,
    event_name: str,
    current_snapshots: list[dict],   # recent snapshots from DB or in-memory
    window_minutes: int = 30,
) -> list[LineMovementAlert]:
    """
    Compare current odds to odds from `window_minutes` ago.
    Returns list of detected movements.
    """
    alerts = []
    cutoff = et_naive() - timedelta(minutes=window_minutes)

    # Group snapshots by market+selection+book
    by_key: dict[str, list[dict]] = {}
    for snap in current_snapshots:
        key = f"{snap['market']}|{snap['selection']}|{snap['book']}"
        by_key.setdefault(key, []).append(snap)

    for key, snaps in by_key.items():
        if len(snaps) < 2:
            continue
        snaps_sorted = sorted(snaps, key=lambda x: x.get("captured_at") or "")
        oldest = snaps_sorted[0]
        newest = snaps_sorted[-1]

        p_old = implied_prob(oldest["american_odds"])
        p_new = implied_prob(newest["american_odds"])
        delta = p_new - p_old   # positive = odds tightening (more likely to move against us)

        if abs(delta) < 0.02:   # below noise floor
            continue

        market, selection, book = key.split("|")

        movement_type = _classify_movement(delta, snaps_sorted)
        alerts.append(LineMovementAlert(
            game_id      = game_id,
            event_name   = event_name,
            market       = market,
            selection    = selection,
            book         = book,
            odds_before  = oldest["american_odds"],
            odds_after   = newest["american_odds"],
            delta_pct    = round(abs(delta), 4),
            movement_type= movement_type,
            detected_at  = et_naive(),
        ))

    return alerts


def _classify_movement(delta: float, snaps: list[dict]) -> str:
    """Classify a line movement by its characteristics."""
    if abs(delta) >= STEAM_THRESHOLD_PCT and len(snaps) <= 3:
        return "steam"     # large rapid move
    if delta < 0 and abs(delta) >= SHARP_THRESHOLD_PCT:
        return "sharp"     # odds improving (sharp money on this side)
    if delta > 0:
        return "public"    # odds shortening (public money)
    return "reverse"


def save_movement(alert: LineMovementAlert) -> None:
    from src.db.session import get_db
    from src.db.models import LineMovement
    with get_db() as db:
        db.add(LineMovement(
            game_id       = int(alert.game_id) if isinstance(alert.game_id, int) else (
                                int(alert.game_id) if isinstance(alert.game_id, str) and alert.game_id.lstrip("-").isdigit() else None
                            ),
            market        = alert.market,
            selection     = alert.selection,
            book          = alert.book,
            odds_before   = alert.odds_before,
            odds_after    = alert.odds_after,
            implied_before= implied_prob(alert.odds_before),
            implied_after = implied_prob(alert.odds_after),
            delta_pct     = alert.delta_pct,
            movement_type = alert.movement_type,
        ))


def score_for_confidence(game_id: int | str, market: str, selection: str, hours: int = 6) -> float:
    """
    Returns a 0-1 score based on recent line movement direction.
    Sharp money aligned with our bet = higher score.
    """
    try:
        from src.db.session import get_db
        from src.db.models import LineMovement

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        with get_db() as db:
            rows = db.query(LineMovement).filter(
                LineMovement.market    == market,
                LineMovement.selection == selection,
                LineMovement.detected_at >= cutoff,
            ).all()
            # extract inside session to avoid DetachedInstanceError
            move_types = [m.movement_type for m in rows]

        if not move_types:
            return 0.5  # neutral

        sharp_moves = sum(1 for t in move_types if t in ("sharp", "steam"))
        public_moves = sum(1 for t in move_types if t == "public")

        if sharp_moves + public_moves == 0:
            return 0.5
        score = 0.5 + (sharp_moves - public_moves) / (sharp_moves + public_moves) * 0.4
        return max(0.1, min(0.9, round(score, 2)))
    except Exception as e:
        logger.warning("Line movement score failed: %s", e)
        return 0.5
