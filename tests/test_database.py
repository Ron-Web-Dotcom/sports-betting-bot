"""Tests for SQLite database layer."""
import os
import pytest
import tempfile
from unittest.mock import patch


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with patch("config.settings.DB_PATH", db_path), \
         patch("src.core.database.DB_PATH", db_path):
        from src.core.database import init_db
        init_db()
        yield db_path


def test_insert_and_retrieve_bet():
    from src.core.database import insert_bet, get_open_bets
    bet = {
        "platform": "draftkings",
        "sport": "basketball_nba",
        "event_id": "abc123",
        "event_name": "Lakers vs Celtics",
        "market": "h2h",
        "selection": "Lakers",
        "odds": 1.91,
        "stake": 20.0,
        "kelly_fraction": 0.05,
        "ai_confidence": 0.65,
        "edge": 0.04,
        "raw_signal": {"should_bet": True},
    }
    bet_id = insert_bet(bet)
    assert bet_id > 0

    open_bets = get_open_bets()
    assert any(b["id"] == bet_id for b in open_bets)


def test_settle_bet():
    from src.core.database import insert_bet, settle_bet, get_open_bets
    bet = {
        "platform": "fanduel",
        "sport": "americanfootball_nfl",
        "event_id": "xyz456",
        "event_name": "Chiefs vs Bills",
        "market": "spreads",
        "selection": "Chiefs -3",
        "odds": 1.91,
        "stake": 15.0,
        "kelly_fraction": 0.03,
        "ai_confidence": 0.68,
        "edge": 0.05,
        "raw_signal": {},
    }
    bet_id = insert_bet(bet)
    settle_bet(bet_id, "win", 15.0)

    open_bets = get_open_bets()
    assert not any(b["id"] == bet_id for b in open_bets)


def test_bankroll_history():
    from src.core.database import record_bankroll, latest_bankroll
    record_bankroll(1000.0, "initial")
    record_bankroll(1050.0, "after win")
    assert latest_bankroll() == pytest.approx(1050.0)


def test_bet_stats():
    from src.core.database import insert_bet, settle_bet, get_bet_stats
    for i in range(3):
        bet = {
            "platform": "prizepicks",
            "sport": "basketball_nba",
            "event_id": f"stat_test_{i}",
            "event_name": f"Game {i}",
            "market": "player_prop",
            "selection": f"Player {i}",
            "odds": 1.9,
            "stake": 10.0,
            "kelly_fraction": 0.02,
            "ai_confidence": 0.65,
            "edge": 0.04,
            "raw_signal": {},
        }
        bet_id = insert_bet(bet)
        result = "win" if i % 2 == 0 else "loss"
        settle_bet(bet_id, result, 10.0)

    stats = get_bet_stats()
    assert stats["total"] == 3
