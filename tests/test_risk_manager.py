"""Tests for Kelly sizing, portfolio risk management."""
import pytest
from unittest.mock import patch
from src.core.risk_manager import (
    kelly_fraction, fractional_kelly, compute_edge,
    size_bet, parlay_ev, parlay_win_prob, size_parlay,
)


class TestKelly:
    def test_positive_ev(self):
        fk = kelly_fraction(0.55, 2.0)
        assert fk == pytest.approx(0.10, abs=0.01)

    def test_negative_ev(self):
        assert kelly_fraction(0.45, 2.0) < 0

    def test_fractional_capped(self):
        full = kelly_fraction(0.60, 2.0)
        frac = fractional_kelly(0.60, 2.0, fraction=0.25)
        assert frac == pytest.approx(full * 0.25, rel=0.01)

    def test_zero_b(self):
        assert kelly_fraction(0.9, 1.0) == 0.0


class TestEdge:
    def test_positive(self):   assert compute_edge(0.55, 2.0) > 0
    def test_negative(self):   assert compute_edge(0.40, 2.0) < 0
    def test_breakeven(self):  assert abs(compute_edge(0.50, 2.0)) < 0.001


class TestParlayMath:
    def test_win_prob_multiplicative(self):
        assert parlay_win_prob([0.6, 0.6]) == pytest.approx(0.36)

    def test_parlay_ev_positive(self):
        combined_dec = 2.0 * 2.0  # two +100 legs
        ev = parlay_ev([0.6, 0.6], combined_dec)
        assert ev > 0  # 36% * 3 - 64% = positive

    def test_parlay_ev_negative_bad_legs(self):
        combined_dec = 2.0 * 2.0
        ev = parlay_ev([0.45, 0.45], combined_dec)
        assert ev < 0


class TestSizeBet:
    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.0)
    def test_approves_good_bet(self, _exp, _br):
        result = size_bet(0.60, 2.0)
        assert result["approved"] is True
        assert 0 < result["stake"] <= 50.0  # max 5% of $1000

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.0)
    def test_rejects_low_confidence(self, _exp, _br):
        result = size_bet(0.50, 2.0)
        assert result["approved"] is False

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.25)
    def test_rejects_full_exposure(self, _exp, _br):
        result = size_bet(0.65, 2.5)
        assert result["approved"] is False

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.0)
    def test_stake_capped(self, _exp, _br):
        result = size_bet(0.90, 5.0)
        if result["approved"]:
            assert result["stake"] <= 50.0


class TestSizeParlay:
    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.0)
    def test_parlay_approved(self, _exp, _br):
        result = size_parlay([0.65, 0.65], 4.0)
        assert result["approved"] is True
        assert result["stake"] <= 20.0  # max 2% of $1000

    @patch("src.core.risk_manager.get_bankroll", return_value=1000.0)
    @patch("src.core.risk_manager.current_portfolio_exposure", return_value=0.0)
    def test_parlay_rejected_bad_ev(self, _exp, _br):
        result = size_parlay([0.45, 0.45], 4.0)
        assert result["approved"] is False
