"""News/injury worker — fetches ESPN data for all configured sports."""
import logging
from src.workers.celery_app import app
from src.engines.news_engine import fetch_all_injuries, save_injuries

logger = logging.getLogger(__name__)


def _is_sleep_time() -> bool:
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_and_save_news(self):
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}
    try:
        injuries_by_sport = fetch_all_injuries()   # dict[str, list[dict]]
        # Flatten to list[dict] before saving — save_injuries expects a flat list
        flat_injuries = [inj for lst in injuries_by_sport.values() for inj in lst]
        save_injuries(flat_injuries)
        logger.info("News worker: saved %d injury/news items", len(flat_injuries))
        return {"injuries": len(flat_injuries)}
    except Exception as exc:
        logger.error("News fetch failed: %s", exc)
        raise self.retry(exc=exc)
