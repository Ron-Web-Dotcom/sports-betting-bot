"""Tests for self_improvement_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.self_improvement_engine import (
    compute_roi_by_dimension, run_full_self_improvement, get_top_performing_categories,
)


def _make_pick(result="won", pnl=0.909, units=1, sport="nba", market="h2h",
               best_book="draftkings", ev_pct=0.05, confidence_pct=0.65):
    p = MagicMock()
    p.result = result
    p.actual_pnl_units = pnl
    p.units = units
    p.sport = sport
    p.market = market
    p.best_book = best_book
    p.ev_pct = ev_pct
    p.confidence_pct = confidence_pct
    return p


def test_compute_roi_empty_returns_empty():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = []
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = compute_roi_by_dimension("sport")
    assert result == []


def test_compute_roi_by_sport():
    picks = [
        _make_pick("won", 0.909, 1, sport="nba"),
        _make_pick("won", 0.909, 1, sport="nba"),
        _make_pick("lost", -1.0, 1, sport="nfl"),
    ]
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = picks
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = compute_roi_by_dimension("sport")

    assert len(result) == 2
    nba = next(r for r in result if r["value"] == "nba")
    assert nba["wins"] == 2
    assert nba["losses"] == 0
    # ROI = net / total_wagered (not / units_lost)
    assert nba["roi"] == pytest.approx((0.909 * 2) / 2, abs=0.01)


def test_compute_roi_uses_total_wagered_not_units_lost():
    """Regression: ROI denominator must be total_wagered, not units_lost."""
    picks = [
        _make_pick("won", 0.909, 1, sport="nba"),
        _make_pick("lost", -1.0, 1, sport="nba"),
    ]
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = picks
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = compute_roi_by_dimension("sport")

    nba = result[0]
    expected_roi = (0.909 - 1.0) / 2  # net / total_wagered(=2 units)
    assert nba["roi"] == pytest.approx(expected_roi, abs=0.01)
    assert nba["total_wagered"] == 2.0


def test_compute_roi_sorted_by_roi_desc():
    picks = [
        _make_pick("won", 2.0, 1, sport="nba"),   # high ROI
        _make_pick("lost", -1.0, 1, sport="nfl"),  # negative ROI
        _make_pick("lost", -1.0, 1, sport="nfl"),
    ]
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = picks
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = compute_roi_by_dimension("sport")

    assert result[0]["value"] == "nba"  # highest ROI first


def test_compute_roi_by_market():
    picks = [_make_pick("won", 0.909, 1, market="h2h")]
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = picks
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = compute_roi_by_dimension("market")
    assert result[0]["value"] == "h2h"


def test_get_top_performing_categories():
    row = MagicMock()
    row.dimension = "sport"
    row.dimension_value = "nba"
    row.roi_pct = 0.12
    row.total_picks = 50
    row.hit_rate = 0.55

    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [row]
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = get_top_performing_categories()

    assert "sport:nba" in result
    assert result["sport:nba"]["roi"] == 0.12


def test_run_full_self_improvement_returns_dict():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.all.return_value = []
    ms.query.return_value.filter_by.return_value.first.return_value = None
    with patch("src.engines.self_improvement_engine.get_db", return_value=ms):
        result = run_full_self_improvement()
    assert isinstance(result, dict)
