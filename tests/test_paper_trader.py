"""Tests for paper trader."""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with patch("config.settings.DB_PATH", db_path), \
         patch("src.core.database.DB_PATH", db_path):
        from src.core.database import init_db
        init_db()
        yield db_path


def _make_trader():
    with patch("config.settings.PAPER_TRADING", True):
        from src.core.paper_trader import PaperTrader
        return PaperTrader()


def test_place_bet_returns_id():
    trader = _make_trader()
    with patch("src.core.paper_trader.insert_bet", return_value=42) as mock_insert:
        bet_id = trader.place_bet(
            platform="prizepicks",
            sport="basketball_nba",
            event_id="ev1",
            event_name="Lakers vs Celtics",
            market="h2h",
            selection="Lakers",
            american_odds=-110,
            stake=10.0,
            kelly_fraction=0.03,
            ai_confidence=0.65,
            edge=0.04,
            raw_signal={},
        )
    assert bet_id == 42
    assert mock_insert.called


def test_settle_updates_bankroll():
    trader = _make_trader()
    with patch("src.core.paper_trader.settle_bet") as mock_settle, \
         patch("src.core.paper_trader.update_bankroll_after_bet") as mock_update:
        trader.settle(1, "win", 1.91, 10.0)
        mock_settle.assert_called_once_with(1, "win", pytest.approx(9.1, abs=0.1))
        mock_update.assert_called_once()
