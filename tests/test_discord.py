"""Tests for Discord alert payload construction."""
from unittest.mock import patch, MagicMock
from src.alerts.discord import (
    alert_bet_placed, alert_parlay_placed, alert_bet_settled,
    alert_arb, alert_session_summary,
)
from src.core.arb_detector import ArbOpportunity


@patch("src.alerts.discord._post")
def test_alert_bet_placed(mock_post):
    alert_bet_placed(
        event_name="Lakers vs Celtics", selection="Lakers", market="h2h",
        american_odds=-110, stake=25.0, edge=0.05, confidence=0.65,
        book="draftkings", reasoning="Strong home record.",
        key_factors=["Home advantage", "Injury to PG"],
        signal_type="value",
    )
    mock_post.assert_called_once()
    embed = mock_post.call_args[0][0]["embeds"][0]
    assert "Straight Bet" in embed["title"]
    names = [f["name"] for f in embed["fields"]]
    assert "Stake" in names and "Edge" in names


@patch("src.alerts.discord._post")
def test_alert_parlay_placed(mock_post):
    from src.core.signal_engine import Signal
    from src.core.parlay_builder import ParlayCandidate
    legs = []
    for i in range(2):
        s = Signal(
            sport="basketball_nba", event_id=str(i), event_name=f"Game {i}",
            market="h2h", selection=f"Team{i}", book="draftkings",
            american_odds=110, win_probability=0.65, ai_confidence=0.68,
            composite_score=0.67, edge=0.05,
        )
        legs.append(s)
    candidate = ParlayCandidate(
        legs=legs, combined_decimal=4.84, win_probability=0.42,
        ev=0.04, sizing={"stake": 10.0, "kelly_fraction": 0.02, "edge": 0.04},
    )
    alert_parlay_placed(candidate, parlay_id=1)
    mock_post.assert_called_once()
    embed = mock_post.call_args[0][0]["embeds"][0]
    assert "Parlay" in embed["title"]


@patch("src.alerts.discord._post")
def test_alert_bet_settled_win(mock_post):
    alert_bet_settled(
        event_name="Chiefs vs Bills", selection="Chiefs",
        result="win", pnl=22.73, stake=25.0, new_bankroll=1022.73,
    )
    embed = mock_post.call_args[0][0]["embeds"][0]
    assert "WON" in embed["title"]


@patch("src.alerts.discord._post")
def test_alert_arb(mock_post):
    arb = ArbOpportunity(
        event_id="e1", event_name="Lakers vs Celtics", sport="basketball_nba",
        market="h2h", book_a="draftkings", selection_a="Lakers", american_odds_a=130,
        book_b="fanduel", selection_b="Celtics", american_odds_b=125,
        guaranteed_profit_pct=0.025, stake_a=46.0, stake_b=54.0,
    )
    alert_arb(arb)
    mock_post.assert_called_once()
    embed = mock_post.call_args[0][0]["embeds"][0]
    assert "ARB" in embed["title"]


@patch("src.alerts.discord._post")
def test_alert_session_summary(mock_post):
    alert_session_summary(
        "Great session — 3W 1L", {"total": 4, "wins": 3, "losses": 1, "total_pnl": 45.0}
    )
    mock_post.assert_called_once()
