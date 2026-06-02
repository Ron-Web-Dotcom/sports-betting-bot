"""Odds worker — scans all sports and persists snapshots."""
import logging
from src.workers.celery_app import app
from src.engines.odds_engine import run_full_odds_scan, get_latest_snapshots_by_game

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=30)
def scan_and_save_odds(self):
    try:
        snapshots = run_full_odds_scan()
        logger.info("Odds scan complete: %d events", len(snapshots))

        # Detect line movements per game using DB snapshots (require captured_at for ordering)
        all_movements = []
        try:
            from src.engines.line_movement_engine import detect_movements, save_movement
            from src.db.session import get_db
            from src.db.models import OddsSnapshot, Game
            from datetime import datetime, timedelta

            with get_db() as db:
                cutoff = datetime.utcnow() - timedelta(hours=2)
                recent = (
                    db.query(OddsSnapshot, Game.external_id, Game.home_team, Game.away_team)
                    .join(Game, OddsSnapshot.game_id == Game.id)
                    .filter(OddsSnapshot.captured_at >= cutoff)
                    .all()
                )

            # Group snapshots by game_id
            by_game: dict[int, dict] = {}
            for snap, ext_id, home, away in recent:
                gid = snap.game_id
                if gid not in by_game:
                    by_game[gid] = {
                        "event_name": f"{away} vs {home}",
                        "snaps": [],
                    }
                by_game[gid]["snaps"].append({
                    "market":        snap.market,
                    "selection":     snap.selection,
                    "book":          snap.book,
                    "american_odds": snap.american_odds,
                    "captured_at":   snap.captured_at,
                })

            for game_id, info in by_game.items():
                movements = detect_movements(
                    game_id          = game_id,
                    event_name       = info["event_name"],
                    current_snapshots= info["snaps"],
                )
                for mv in movements:
                    save_movement(mv)
                all_movements.extend(movements)

        except Exception as mv_exc:
            logger.warning("Line movement detection failed: %s", mv_exc)

        if all_movements:
            from src.workers.alert_worker import send_line_movement_alerts
            import dataclasses
            send_line_movement_alerts.delay([dataclasses.asdict(m) for m in all_movements])

        return {"snapshots": len(snapshots), "movements": len(all_movements)}
    except Exception as exc:
        logger.error("Odds scan failed: %s", exc)
        raise self.retry(exc=exc)
