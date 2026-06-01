"""Tests for Kelly sizing and risk management."""
import pytest
from unittest.mock import patch
from src.core.risk_manager import (
    kelly_fraction,
    fractional_kelly,
    compute_edge,
    size_bet,
)


class TestKelly:
    def test_positive_ev_bet(self):
        # 55% win probability at even odds (2.0 decimal)
        fk = kelly_fraction(0.55, 2.0)
        assert fk == pytest.approx(0.10, abs=0.01)

    def test_negative_ev_returns_negative(self):
        fk = kelly_fraction(0.45, 2.0)
        assert fk < 0

    def test_fractional_kelly_caps(self):
        fk_full = kelly_fraction(0.60, 2.0)
        fk_frac = fractional_kelly(0.60, 2.0, fraction=0.25)
        assert fk_frac == pytest.approx(fk_full * 0.25, rel=0.01)

    def test_zero_odds_returns_zero(self):
        assert kelly_fraction(0.9, 1.0) == 0.0


class TestEdge:
    def test_positive_edge(self):
        edge = compute_edge(0.55, 2.0)
        assert edge > 0

    def test_negative_edge(self):
        edge = compute_edge(0.40, 2.0)
        assert edge < 0

    def test_breakeven_edge(self):
        # win_prob = 0.5 at 2.0 decimal = exactly breakeven
        edge = compute_edge(0.50, 2.0)
        assert abs(edge) < 0.001


class TestSizeBet:
    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    def test_approves_good_bet(self, _mock):
        result = size_bet(0.60, 2.0)
        assert result["approved"] is True
        assert result["stake"] > 0
        assert result["stake"] <= 50.0  # max 5% of $1000

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    def test_rejects_low_confidence(self, _mock):
        result = size_bet(0.50, 2.0)   # below MIN_CONFIDENCE=0.60
        assert result["approved"] is False

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    def test_rejects_negative_ev(self, _mock):
        result = size_bet(0.61, 1.5)  # negative EV at these params
        assert result["approved"] is False or result["edge"] < 0

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    def test_stake_capped_at_max_pct(self, _mock):
        # Very high confidence / high odds — Kelly would normally recommend big bet
        result = size_bet(0.90, 5.0)
        if result["approved"]:
            assert result["stake"] <= 50.0  # 5% of $1000
