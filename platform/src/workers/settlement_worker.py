"""Settlement worker — settles picks and records CLV."""
import logging
from datetime import datetime, timedelta
from src.workers.celery_app import app
from src.db.session import get_db
from src.db.models import Pick, Game

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=120)
def settle_completed_picks(self):
    """
    Fetch completed games, match against open picks, settle W/L/Push,
    compute actual P&L, and fire result alerts.
    """
    try:
        from src.engines.odds_engine import fetch_scores
        settled_count = 0

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

        with get_db() as db:
            for pick in open_picks:
                score = scores.get(pick.game_id)
                if not score or not score.get("completed"):
                    continue

                winner = score.get("winner")
                result = _determine_result(pick, winner, score)
                if not result:
                    continue

                pnl = _calculate_pnl(pick, result)
                db_pick = db.query(Pick).filter_by(id=pick.id).first()
                if db_pick:
                    db_pick.result = result
                    db_pick.actual_pnl_units = pnl
                    db_pick.settled_at = datetime.utcnow()
                    settled_count += 1

                    from src.workers.alert_worker import send_result_alert
                    send_result_alert.delay(
                        {
                            "bet": db_pick.selection,
                            "sport": db_pick.sport,
                            "odds": db_pick.american_odds_at_gen,
                            "actual_pnl_units": pnl,
                        },
                        result,
                    )

        logger.info("Settled %d picks", settled_count)
        return {"settled": settled_count}

    except Exception as exc:
        logger.error("Settlement failed: %s", exc)
        raise self.retry(exc=exc)


def _determine_result(pick: Pick, winner: str | None, score: dict) -> str | None:
    """Map game outcome to pick result."""
    if not winner:
        return None
    selection = (pick.selection or "").lower()
    winner_lower = winner.lower()
    if winner_lower in selection or selection in winner_lower:
        return "won"
    if score.get("push"):
        return "push"
    return "lost"


def _calculate_pnl(pick: Pick, result: str) -> float:
    units = pick.units or 1
    if result == "won":
        from src.engines.ev_engine import american_to_decimal
        dec = american_to_decimal(pick.american_odds_at_gen or -110)
        return round((dec - 1) * units, 2)
    elif result == "lost":
        return -units
    return 0.0


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
