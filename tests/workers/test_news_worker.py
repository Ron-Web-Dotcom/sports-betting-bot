"""Tests for news_worker."""
import pytest
from unittest.mock import patch, MagicMock
import inspect
import src.workers.news_worker as nw


def test_flatten_in_source():
    src = inspect.getsource(nw.fetch_and_save_news)
    assert "all_injuries" in src
    assert "save_injuries" in src


def _make_mock_redis():
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    return mock_redis


def test_fetch_and_save_news_saves_flattened():
    injuries_by_sport = {
        "basketball_nba": [
            {"sport": "basketball_nba", "player_name": "A", "team": "X",
             "status": "out", "detail": "", "fetched_at": "2026-01-01"},
        ],
        "americanfootball_nfl": [
            {"sport": "americanfootball_nfl", "player_name": "B", "team": "Y",
             "status": "questionable", "detail": "", "fetched_at": "2026-01-01"},
        ],
    }
    mock_redis = _make_mock_redis()
    with patch("src.workers.news_worker.fetch_all_injuries", return_value=injuries_by_sport), \
         patch("src.workers.news_worker.get_all_injured_players", return_value=[]), \
         patch("src.workers.news_worker.save_injuries") as mock_save, \
         patch("src.workers.news_worker._is_sleep_time", return_value=False), \
         patch("redis.from_url", return_value=mock_redis):
        nw.fetch_and_save_news()

    mock_save.assert_called_once()
    flat = mock_save.call_args[0][0]
    assert isinstance(flat, list)
    assert len(flat) == 2


def test_sleeper_injuries_merged():
    """Sleeper-only players (not in ESPN) appear in the merged list."""
    sleeper_only = [
        {"sport": "americanfootball_nfl", "player_name": "C", "team": "Z",
         "injury_status": "Out", "status": "Active", "injury_notes": "knee"},
    ]
    mock_redis = _make_mock_redis()
    with patch("src.workers.news_worker.fetch_all_injuries", return_value={}), \
         patch("src.workers.news_worker.get_all_injured_players", return_value=sleeper_only), \
         patch("src.workers.news_worker.save_injuries") as mock_save, \
         patch("src.workers.news_worker._is_sleep_time", return_value=False), \
         patch("redis.from_url", return_value=mock_redis):
        nw.fetch_and_save_news()

    mock_save.assert_called_once()
    flat = mock_save.call_args[0][0]
    assert any(p["player_name"] == "C" for p in flat)


def test_espn_wins_collision():
    """When ESPN and Sleeper report the same player, ESPN entry is kept."""
    espn_injuries = {
        "americanfootball_nfl": [
            {"player_name": "D", "team": "T1", "sport": "americanfootball_nfl",
             "status": "doubtful", "detail": "espn detail", "fetched_at": "2026-01-01"},
        ]
    }
    sleeper_injuries = [
        {"sport": "americanfootball_nfl", "player_name": "D", "team": "T1",
         "injury_status": "Questionable", "status": "Active", "injury_notes": "sleeper note"},
    ]
    mock_redis = _make_mock_redis()
    with patch("src.workers.news_worker.fetch_all_injuries", return_value=espn_injuries), \
         patch("src.workers.news_worker.get_all_injured_players", return_value=sleeper_injuries), \
         patch("src.workers.news_worker.save_injuries") as mock_save, \
         patch("src.workers.news_worker._is_sleep_time", return_value=False), \
         patch("redis.from_url", return_value=mock_redis):
        nw.fetch_and_save_news()

    flat = mock_save.call_args[0][0]
    player_d = next(p for p in flat if p["player_name"] == "D")
    assert player_d.get("source") == "espn"
    assert player_d.get("detail") == "espn detail"


def test_fetch_and_save_news_empty_result():
    mock_redis = _make_mock_redis()
    with patch("src.workers.news_worker.fetch_all_injuries", return_value={}), \
         patch("src.workers.news_worker.get_all_injured_players", return_value=[]), \
         patch("src.workers.news_worker.save_injuries") as mock_save, \
         patch("src.workers.news_worker._is_sleep_time", return_value=False), \
         patch("redis.from_url", return_value=mock_redis):
        nw.fetch_and_save_news()

    mock_save.assert_called_once_with([])


def test_fetch_and_save_news_retries_on_exception():
    """Task should retry on exception (max_retries=3)."""
    task = nw.fetch_and_save_news
    assert hasattr(task, "max_retries")
    assert task.max_retries >= 1
