"""Tests for news_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.news_engine import (
    fetch_injuries, fetch_news, score_injury_impact, score_news_impact,
    save_injuries, fetch_all_injuries, INJURY_CRITICAL_STATUSES,
)


def test_fetch_injuries_unknown_sport():
    result = fetch_injuries("unknown_sport_xyz")
    assert result == []


def test_fetch_injuries_espn_error():
    with patch("src.engines.news_engine._espn_get", return_value=None):
        result = fetch_injuries("basketball_nba")
    assert result == []


def test_fetch_injuries_parses_athlete():
    mock_data = {
        "injuries": [{
            "athlete": {"displayName": "LeBron James"},
            "team": {"displayName": "Los Angeles Lakers"},
            "status": "out",
            "longComment": "Ankle sprain",
        }]
    }
    with patch("src.engines.news_engine._espn_get", return_value=mock_data):
        result = fetch_injuries("basketball_nba")
    assert len(result) == 1
    assert result[0]["player_name"] == "LeBron James"
    assert result[0]["status"] == "out"
    assert result[0]["sport"] == "basketball_nba"
    assert result[0]["team"] == "Los Angeles Lakers"
    assert result[0]["detail"] == "Ankle sprain"


def test_fetch_injuries_empty_list():
    with patch("src.engines.news_engine._espn_get", return_value={"injuries": []}):
        result = fetch_injuries("basketball_nba")
    assert result == []


def test_fetch_news_unsupported_sport():
    result = fetch_news("soccer_epl")
    assert result == []


def test_fetch_news_api_error():
    with patch("src.engines.news_engine._espn_get", return_value=None):
        result = fetch_news("basketball_nba")
    assert result == []


def test_fetch_news_parses_articles():
    mock_data = {
        "articles": [
            {"headline": "Lakers Win", "description": "They won", "published": "2026-01-01"},
            {"headline": "Bulls Trade", "description": "Trade", "published": "2026-01-02"},
        ] * 10   # 20 articles
    }
    with patch("src.engines.news_engine._espn_get", return_value=mock_data):
        result = fetch_news("basketball_nba", limit=5)
    assert len(result) == 5
    assert result[0]["headline"] == "Lakers Win"
    assert result[0]["sport"] == "basketball_nba"


def test_score_injury_impact_no_injuries():
    score = score_injury_impact([], "Lakers")
    assert score == 0.5


def test_score_injury_impact_team_not_found():
    injuries = [{"team": "Celtics", "status": "out"}]
    score = score_injury_impact(injuries, "Lakers")
    assert score == 0.5


def test_score_injury_impact_one_critical():
    injuries = [{"team": "Los Angeles Lakers", "status": "out"}]
    score = score_injury_impact(injuries, "Lakers")
    assert score < 0.5


def test_score_injury_impact_multiple_critical():
    injuries = [
        {"team": "Los Angeles Lakers", "status": "out"},
        {"team": "Los Angeles Lakers", "status": "doubtful"},
        {"team": "Los Angeles Lakers", "status": "questionable"},
    ]
    score = score_injury_impact(injuries, "Lakers")
    assert score <= 0.3   # 3 critical injuries reduces well below 0.5


def test_score_injury_impact_non_critical():
    injuries = [{"team": "Los Angeles Lakers", "status": "active"}]
    score = score_injury_impact(injuries, "Lakers")
    assert score == 0.5  # no critical injuries


def test_score_news_impact_always_neutral():
    assert score_news_impact([], "Lakers") == 0.5
    assert score_news_impact([{"headline": "test"}], "Lakers") == 0.5


def test_save_injuries_calls_db_add():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    with patch("src.db.session.get_db", return_value=ms):
        save_injuries([
            {"sport": "basketball_nba", "player_name": "Test Player",
             "team": "Lakers", "status": "out", "detail": "Knee",
             "fetched_at": "2026-01-01"},
        ])
    assert ms.add.called


def test_save_injuries_critical_impact_score():
    """Out/doubtful injuries should get 0.6 impact, non-critical 0.3."""
    added_objs = []
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.add.side_effect = lambda o: added_objs.append(o)
    with patch("src.db.session.get_db", return_value=ms):
        save_injuries([
            {"sport": "nba", "player_name": "A", "team": "X",
             "status": "out", "detail": "", "fetched_at": "2026-01-01"},
            {"sport": "nba", "player_name": "B", "team": "X",
             "status": "active", "detail": "", "fetched_at": "2026-01-01"},
        ])
    assert added_objs[0].impact_score == 0.6
    assert added_objs[1].impact_score == 0.3


def test_fetch_all_injuries_flattens():
    with patch("src.engines.news_engine.fetch_injuries") as mock_fetch:
        mock_fetch.return_value = [{"sport": "nba", "player_name": "X", "team": "Y",
                                    "status": "out", "detail": "", "fetched_at": ""}]
        result = fetch_all_injuries()
    assert isinstance(result, dict)
    for v in result.values():
        assert isinstance(v, list)


def test_fetch_all_injuries_skips_empty():
    with patch("src.engines.news_engine.fetch_injuries", return_value=[]):
        result = fetch_all_injuries()
    assert result == {}
