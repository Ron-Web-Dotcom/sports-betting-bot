"""News/injury worker — fetches ESPN data for all configured sports."""
import logging
from src.workers.celery_app import app
from src.engines.news_engine import fetch_all_injuries, save_injuries

logger = logging.getLogger(__name__)


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_and_save_news(self):
    try:
        injuries = fetch_all_injuries()
        save_injuries(injuries)
        logger.info("News worker: saved %d injury/news items", len(injuries))
        return {"injuries": len(injuries)}
    except Exception as exc:
        logger.error("News fetch failed: %s", exc)
        raise self.retry(exc=exc)
