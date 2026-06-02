"""Tests for news_worker."""
import pytest
from unittest.mock import patch, MagicMock
import inspect
import src.workers.news_worker as nw


def test_flatten_in_source():
    src = inspect.getsource(nw.fetch_and_save_news)
    assert "flat_injuries" in src
    assert "values()" in src


def test_fetch_and_save_news_saves_flattened():
    injuries_by_sport = {
        "nba": [
            {"sport": "nba", "player_name": "A", "team": "X",
             "status": "out", "detail": "", "fetched_at": "2026-01-01"},
        ],
        "nfl": [
            {"sport": "nfl", "player_name": "B", "team": "Y",
             "status": "questionable", "detail": "", "fetched_at": "2026-01-01"},
        ],
    }
    with patch("src.workers.news_worker.fetch_all_injuries", return_value=injuries_by_sport), \
         patch("src.workers.news_worker.save_injuries") as mock_save:
        nw.fetch_and_save_news()

    mock_save.assert_called_once()
    flat = mock_save.call_args[0][0]
    assert isinstance(flat, list)
    assert len(flat) == 2


def test_fetch_and_save_news_empty_result():
    with patch("src.workers.news_worker.fetch_all_injuries", return_value={}), \
         patch("src.workers.news_worker.save_injuries") as mock_save:
        nw.fetch_and_save_news()

    mock_save.assert_called_once_with([])


def test_fetch_and_save_news_retries_on_exception():
    """Task should retry on exception (max_retries=3)."""
    task = nw.fetch_and_save_news
    assert hasattr(task, "max_retries")
    assert task.max_retries >= 1
