"""Tests for Discord alert formatting."""
from unittest.mock import patch, MagicMock
from src.alerts.discord import alert_bet_placed, alert_bet_settled, alert_session_summary


@patch("src.alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
@patch("src.alerts.discord._post")
def test_alert_bet_placed(mock_post):
    alert_bet_placed(
        event_name="Lakers vs Celtics",
        selection="Lakers",
        market="h2h",
        american_odds=-110,
        stake=25.0,
        edge=0.05,
        confidence=0.65,
        platform="draftkings",
        reasoning="Strong home record and opponent injuries.",
        key_factors=["Home advantage", "Injury to opposing PG"],
    )
    mock_post.assert_called_once()
    payload = mock_post.call_args[0][0]
    embed = payload["embeds"][0]
    assert "New Bet Placed" in embed["title"]
    field_names = [f["name"] for f in embed["fields"]]
    assert "Stake" in field_names
    assert "Edge" in field_names


@patch("src.alerts.discord._post")
def test_alert_bet_settled_win(mock_post):
    alert_bet_settled(
        event_name="Chiefs vs Bills",
        selection="Chiefs",
        result="win",
        pnl=22.73,
        stake=25.0,
        new_bankroll=1022.73,
    )
    mock_post.assert_called_once()
    embed = mock_post.call_args[0][0]["embeds"][0]
    assert "Won" in embed["title"]


@patch("src.alerts.discord._post")
def test_alert_session_summary(mock_post):
    alert_session_summary(
        "Strong session. 3 wins, 1 loss.",
        {"total": 4, "wins": 3, "losses": 1, "total_pnl": 45.0},
    )
    mock_post.assert_called_once()
