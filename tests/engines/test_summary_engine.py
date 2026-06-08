"""Tests for summary_engine."""
import os
os.environ.setdefault("USE_SQLITE", "true")

import pytest
from unittest.mock import patch, MagicMock
from src.engines.summary_engine import get_daily_summary, get_weekly_summary, get_monthly_summary


def _mock_pick(result="won", sport="nba", market="moneyline", book="draftkings",
               pnl=1.0, units=2, ev=0.05, conf=65.0, clv=0.02, settled_at=None):
    from datetime import datetime
    p = MagicMock()
    p.result = result
    p.sport = sport
    p.market = market
    p.best_book = book
    p.actual_pnl_units = pnl
    p.units = units
    p.ev_pct = ev
    p.confidence_pct = conf
    p.clv_pct = clv
    p.selection = "Team A -3.5"
    p.id = 1
    p.recommendation = "BET"
    p.settled_at = settled_at or datetime(2026, 1, 1)
    p.is_parlay_leg = False
    return p


def _make_ctx(picks):
    ctx = MagicMock()
    db = MagicMock()
    query_chain = MagicMock()
    query_chain.filter.return_value = query_chain
    query_chain.all.return_value = picks
    db.query.return_value = query_chain
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


@patch("src.db.session.get_db")
def test_daily_summary_fields(mock_get_db):
    picks = [
        _mock_pick("won", pnl=2.0),
        _mock_pick("lost", pnl=-1.0),
        _mock_pick("push", pnl=0.0),
    ]
    mock_get_db.return_value = _make_ctx(picks)

    result = get_daily_summary()

    for key in ("total_picks", "wins", "losses", "pushes", "win_rate", "units_won",
                "units_lost", "net_units", "net_profit", "roi", "avg_ev",
                "avg_confidence", "avg_clv", "best_pick", "worst_pick",
                "best_sport", "worst_sport", "best_sportsbook", "most_profitable_market"):
        assert key in result, f"Missing key: {key}"


@patch("src.db.session.get_db")
def test_daily_summary_empty(mock_get_db):
    mock_get_db.return_value = _make_ctx([])

    result = get_daily_summary()
    assert result["total_picks"] == 0
    assert result["win_rate"] == 0.0
    assert result["best_pick"] is None


@patch("src.db.session.get_db")
def test_weekly_summary_fields(mock_get_db):
    picks = [_mock_pick("won", pnl=3.0), _mock_pick("lost", pnl=-1.0)]
    mock_get_db.return_value = _make_ctx(picks)

    result = get_weekly_summary()
    for key in ("total_bets", "net_units", "roi", "avg_clv", "best_sport",
                "best_sportsbook", "best_prop_category", "best_parlay_category"):
        assert key in result, f"Missing key: {key}"


@patch("src.db.session.get_db")
def test_monthly_summary_streaks(mock_get_db):
    from datetime import datetime
    picks = [
        _mock_pick("won", pnl=1.0, settled_at=datetime(2026, 1, i))
        for i in range(1, 4)
    ] + [_mock_pick("lost", pnl=-1.0, settled_at=datetime(2026, 1, 5))]

    ctx = MagicMock()
    db = MagicMock()
    # first call returns picks, second call for parlays returns []
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = picks
    db.query.return_value.filter.return_value.all.side_effect = [picks, []]
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_get_db.return_value = ctx

    result = get_monthly_summary(2026, 1)
    assert "largest_winning_streak" in result
    assert "largest_losing_streak" in result
    assert result["largest_winning_streak"] >= 0
