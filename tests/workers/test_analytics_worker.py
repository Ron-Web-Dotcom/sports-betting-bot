"""Tests for analytics_worker."""
import pytest
from unittest.mock import patch, MagicMock
import inspect
import src.workers.analytics_worker as aw


def test_send_daily_summary_queries_db():
    src = inspect.getsource(aw.send_daily_summary)
    assert "get_db" in src or "db.query" in src
    assert "picks_dicts = []" not in src


def test_cleanup_task_exists():
    assert hasattr(aw, "cleanup_old_snapshots")


def test_cleanup_old_snapshots_runs():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.delete.return_value = 10

    with patch("src.db.session.get_db", return_value=ms):
        result = aw.cleanup_old_snapshots()

    assert "snapshots_deleted" in result
    assert "alerts_deleted" in result


def test_snapshot_portfolio_saves():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)

    mock_stats = {"period": "lifetime", "total": 0, "wins": 0, "losses": 0,
                  "units_won": 0, "roi": 0.0, "hit_rate": 0.0, "net_units": 0}

    with patch("src.engines.portfolio_engine.get_performance_stats", return_value=mock_stats), \
         patch("src.db.session.get_db", return_value=ms):
        result = aw.snapshot_portfolio()

    assert ms.add.called


def test_cleanup_registered_in_beat():
    from src.workers.celery_app import app
    schedule_str = str(app.conf.beat_schedule)
    assert "cleanup" in schedule_str


def test_send_daily_summary_task_exists():
    from src.workers.celery_app import app
    task_names = list(app.tasks.keys())
    assert any("send_daily_summary" in t for t in task_names)
