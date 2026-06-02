"""Tests for the SQLite database layer."""
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db = str(tmp_path / "test.db")
    with patch("config.settings.DB_PATH", db), patch("src.core.database.DB_PATH", db):
        from src.core.database import init_db
        init_db()
        yield db


def test_insert_and_list_position():
    from src.core.database import insert_position, get_open_positions
    pos = {
        "sport": "basketball_nba", "event_id": "e1", "event_name": "Lakers vs Celtics",
        "market": "h2h", "selection": "Lakers", "book": "draftkings",
        "american_odds": -110, "decimal_odds": 1.909,
        "stake": 20.0, "kelly_fraction": 0.05, "edge": 0.04,
        "ai_confidence": 0.65, "parlay_id": None, "signal_data": {},
    }
    pid = insert_position(pos)
    assert pid > 0
    open_pos = get_open_positions()
    assert any(p["id"] == pid for p in open_pos)


def test_settle_position():
    from src.core.database import insert_position, settle_position, get_open_positions
    pos = {
        "sport": "americanfootball_nfl", "event_id": "e2", "event_name": "Chiefs vs Bills",
        "market": "spreads", "selection": "Chiefs -3", "book": "fanduel",
        "american_odds": -110, "decimal_odds": 1.909,
        "stake": 15.0, "kelly_fraction": 0.03, "edge": 0.05,
        "ai_confidence": 0.68, "parlay_id": None, "signal_data": {},
    }
    pid = insert_position(pos)
    settle_position(pid, "win", 13.63)
    assert not any(p["id"] == pid for p in get_open_positions())


def test_insert_and_list_parlay():
    from src.core.database import insert_parlay, get_open_parlays
    row = {
        "legs": [1, 2, 3], "combined_odds": 6.0,
        "stake": 10.0, "potential_payout": 60.0, "leg_count": 3, "parlay_type": "standard",
    }
    pid = insert_parlay(row)
    assert pid > 0
    parlays = get_open_parlays()
    assert any(p["id"] == pid for p in parlays)
    found = next(p for p in parlays if p["id"] == pid)
    assert found["legs"] == [1, 2, 3]


def test_bankroll_history():
    from src.core.database import record_bankroll, latest_bankroll
    record_bankroll(1000.0, "start")
    record_bankroll(1100.0, "win")
    assert latest_bankroll() == pytest.approx(1100.0)


def test_arb_insert_and_retrieve():
    from src.core.database import insert_arb, get_recent_arbs
    arb = {
        "sport": "basketball_nba", "event_id": "arb1", "event_name": "Lakers vs Celtics",
        "market": "h2h", "book_a": "draftkings", "book_b": "fanduel",
        "odds_a": 120, "odds_b": 105,
        "selection_a": "Lakers", "selection_b": "Celtics",
        "guaranteed_profit_pct": 0.015, "stake_a": 45.0, "stake_b": 55.0,
    }
    aid = insert_arb(arb)
    assert aid > 0
    arbs = get_recent_arbs(hours=1)
    assert any(a["id"] == aid for a in arbs)


def test_signal_insert_and_retrieve():
    from src.core.database import insert_signal, get_top_signals
    sig = {
        "sport": "basketball_nba", "event_id": "s1", "event_name": "Warriors vs Nets",
        "market": "h2h", "selection": "Warriors",
        "line_value": -115.0, "public_bet_pct": None,
        "line_movement": 0.6, "injury_impact": 0.5,
        "ai_confidence": 0.70, "composite_score": 0.67,
        "signal_type": "value", "raw_data": {"key": "val"},
    }
    sid = insert_signal(sig)
    assert sid > 0
    top = get_top_signals(limit=10)
    assert any(s["id"] == sid for s in top)
