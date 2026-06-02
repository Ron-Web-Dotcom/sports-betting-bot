"""Settlement worker — settles picks and records CLV."""
import logging
import re
from datetime import datetime, timedelta
from src.workers.celery_app import app
from src.db.session import get_db
from src.db.models import Pick, Game

logger = logging.getLogger(__name__)

# Team name suffixes to strip when normalising for comparison
_SUFFIXES = re.compile(
    r'\b(fc|city|united|sc|cf|afc|bfc|sporting|athletics)\b', re.IGNORECASE
)


def _normalize_team_name(name: str) -> str:
    """Strip common suffixes and lowercase for fuzzy matching."""
    if not name:
        return ""
    name = _SUFFIXES.sub("", name).strip()
    return re.sub(r'\s+', ' ', name).lower().strip()


@app.task(bind=True, max_retries=3, default_retry_delay=120)
def settle_completed_picks(self):
    """
    Fetch completed games, match against open picks, settle W/L/Push,
    compute actual P&L, and fire result alerts.

    Everything runs in a SINGLE DB session to prevent double-settlement.
    Alerts are fired AFTER the session commits to prevent alert-fires-but-
    commit-fails scenarios.
    """
    try:
        from src.engines.odds_engine import fetch_scores

        alerts_to_send = []

        with get_db() as db:
            open_picks = db.query(Pick).filter(
                Pick.result.is_(None),
                Pick.recommendation == "BET",
                Pick.generated_at >= datetime.utcnow() - timedelta(days=3),
            ).all()

            if not open_picks:
                return {"settled": 0}

            game_ids = list({p.game_id for p in open_picks if p.game_id})
            scores = fetch_scores(game_ids)

            settled_count = 0
            for pick in open_picks:
                score = scores.get(pick.game_id)
                if not score or not score.get("completed"):
                    continue

                winner = score.get("winner")
                result = _determine_result(pick, winner, score)
                if not result:
                    continue

                # Re-fetch within same session to avoid stale state
                db_pick = db.query(Pick).filter_by(id=pick.id).first()
                if not db_pick or db_pick.result is not None:
                    # Already settled — skip to prevent double-settlement
                    continue

                pnl = _calculate_pnl(pick, result)
                db_pick.result = result
                db_pick.actual_pnl_units = pnl
                db_pick.settled_at = datetime.utcnow()
                settled_count += 1

                # Collect data as plain dict BEFORE commit so the session
                # can close cleanly; we fire the alert outside the session.
                alerts_to_send.append((
                    {
                        "bet": db_pick.selection,
                        "sport": db_pick.sport,
                        "odds": db_pick.american_odds_at_gen,
                        "actual_pnl_units": pnl,
                    },
                    result,
                ))

        # Fire alerts AFTER the DB session has committed
        from src.workers.alert_worker import send_result_alert
        for pick_data, result in alerts_to_send:
            try:
                send_result_alert.delay(pick_data, result)
            except Exception as e:
                logger.error("Alert dispatch failed for result %s: %s", result, e)

        logger.info("Settled %d picks", settled_count)
        return {"settled": settled_count}

    except Exception as exc:
        logger.error("Settlement failed: %s", exc)
        raise self.retry(exc=exc)


def _determine_result(pick: Pick, winner: str | None, score: dict) -> str | None:
    """Map game outcome to pick result."""
    # Handle canceled / postponed games
    status = score.get("status", "")
    if status in ("canceled", "postponed"):
        return "void"

    if score.get("push"):
        return "push"

    if not winner:
        return None

    selection_norm = _normalize_team_name(pick.selection or "")
    winner_norm    = _normalize_team_name(winner)

    if winner_norm and selection_norm and (
        winner_norm in selection_norm or selection_norm in winner_norm
    ):
        return "won"

    return "lost"


def _calculate_pnl(pick: Pick, result: str) -> float:
    units = pick.units or 1
    if result == "won":
        from src.engines.ev_engine import american_to_decimal
        dec = american_to_decimal(pick.american_odds_at_gen or -110)
        return round((dec - 1) * units, 2)
    elif result == "lost":
        return -units
    return 0.0  # push or void


@app.task
def record_closing_lines():
    """Snapshot current odds for open picks — used later for CLV calculation."""
    from src.engines.clv_engine import record_clv
    from src.engines.odds_engine import get_latest_snapshots_by_game

    with get_db() as db:
        open_picks = db.query(Pick).filter(
            Pick.result.is_(None),
            Pick.recommendation == "BET",
        ).all()

    if not open_picks:
        return {"recorded": 0}

    snapshots = get_latest_snapshots_by_game()
    recorded = 0

    for pick in open_picks:
        snap_list = snapshots.get(pick.game_id, [])
        if not snap_list:
            continue
        closing_odds = snap_list[0].get("best_odds")
        if closing_odds:
            record_clv(pick.id, pick.american_odds_at_gen, closing_odds)
            recorded += 1

    return {"recorded": recorded}
