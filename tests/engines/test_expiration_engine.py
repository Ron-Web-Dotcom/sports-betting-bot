"""Tests for expiration_engine."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from src.engines.expiration_engine import assess_expiration, format_expiration_embed, ExpirationWindow


def _mock_game(minutes_from_now=120):
    game = MagicMock()
    game.commence_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    game.id = 1
    return game


def _mock_pick(game_id=1):
    p = MagicMock()
    p.game_id = game_id
    return p


def _make_ctx(pick, game):
    ctx = MagicMock()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pick, game]
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


@patch("src.db.session.get_db")
def test_game_imminent_bet_now(mock_get_db):
    mock_get_db.return_value = _make_ctx(_mock_pick(), _mock_game(minutes_from_now=30))
    window = assess_expiration(1, -110, [])
    assert window.urgency == "bet_now"
    assert window.minutes_remaining < 60


@patch("src.db.session.get_db")
def test_no_movements_stable(mock_get_db):
    mock_get_db.return_value = _make_ctx(_mock_pick(), _mock_game(minutes_from_now=180))
    window = assess_expiration(1, -110, [])
    assert window.urgency == "bet_now"


@patch("src.db.session.get_db")
def test_line_moving_against_value_expiring(mock_get_db):
    mock_get_db.return_value = _make_ctx(_mock_pick(), _mock_game(minutes_from_now=180))
    movements = [
        {"odds_before": -120, "odds_after": -115},
        {"odds_before": -115, "odds_after": -110},
        {"odds_before": -110, "odds_after": -105},
    ]
    window = assess_expiration(1, -105, movements)
    assert window.urgency == "value_expiring"


@patch("src.db.session.get_db")
def test_line_moving_in_favor(mock_get_db):
    mock_get_db.return_value = _make_ctx(_mock_pick(), _mock_game(minutes_from_now=180))
    movements = [
        {"odds_before": -105, "odds_after": -110},
        {"odds_before": -110, "odds_after": -115},
        {"odds_before": -115, "odds_after": -120},
    ]
    window = assess_expiration(1, -120, movements)
    assert window.urgency == "better_line_expected"


def test_format_expiration_embed():
    window = ExpirationWindow(
        current_odds=-110,
        expected_odds=-115,
        bet_before=datetime.now(timezone.utc).isoformat(),
        urgency="value_expiring",
        minutes_remaining=45.0,
        reasoning="Test reasoning",
    )
    embed = format_expiration_embed(window)
    assert "urgency" in embed
    assert "current_odds" in embed
    assert "expected_odds" in embed
    assert "bet_before" in embed
    assert "reasoning" in embed
    assert "VALUE EXPIRING" in embed["urgency"]


def test_format_expiration_embed_bet_now():
    window = ExpirationWindow(
        current_odds=+200,
        expected_odds=+200,
        bet_before=datetime.now(timezone.utc).isoformat(),
        urgency="bet_now",
        minutes_remaining=30.0,
        reasoning="Stable",
    )
    embed = format_expiration_embed(window)
    assert "BET NOW" in embed["urgency"]
