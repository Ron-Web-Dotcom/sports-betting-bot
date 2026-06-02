"""Odds worker — scans all sports and persists snapshots."""
import logging
from src.workers.celery_app import app
from src.engines.odds_engine import run_full_odds_scan
from src.engines.comparison_engine import compare_all_markets
from src.engines.line_movement_engine import detect_movements

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=30)
def scan_and_save_odds(self):
    try:
        snapshots = run_full_odds_scan()
        logger.info("Odds scan complete: %d snapshots", len(snapshots))

        movements = detect_movements(snapshots)
        if movements:
            from src.workers.alert_worker import send_line_movement_alerts
            send_line_movement_alerts.delay([m.__dict__ for m in movements])

        return {"snapshots": len(snapshots), "movements": len(movements)}
    except Exception as exc:
        logger.error("Odds scan failed: %s", exc)
        raise self.retry(exc=exc)
