"""
Comprehensive EV Engine tests — all edge cases.
"""
import pytest
from src.engines.ev_engine import (
    american_to_decimal,
    implied_prob,
    remove_vig,
    compute_ev,
    assign_units,
    bankroll_examples,
    evaluate,
)


# ── american_to_decimal ────────────────────────────────────────────────────────

def test_american_to_decimal_zero_no_crash():
    result = american_to_decimal(0)
    assert result == pytest.approx(2.0)


def test_american_to_decimal_minus100():
    assert american_to_decimal(-100) == pytest.approx(2.0)


def test_american_to_decimal_plus100():
    assert american_to_decimal(100) == pytest.approx(2.0)


def test_american_to_decimal_plus999():
    # +999 → 999/100 + 1 = 10.99
    assert american_to_decimal(999) == pytest.approx(10.99)


def test_american_to_decimal_minus999():
    # -999 → 100/999 + 1 ≈ 1.1001
    assert american_to_decimal(-999) == pytest.approx(1.1001, rel=1e-3)


def test_american_to_decimal_very_large_positive():
    # Should not overflow or crash
    result = american_to_decimal(10000)
    assert result == pytest.approx(101.0)
    assert result > 1.0


# ── implied_prob ───────────────────────────────────────────────────────────────

def test_implied_prob_zero_no_crash():
    # odds=0 → american_to_decimal returns 2.0 → implied_prob = 0.5
    result = implied_prob(0)
    assert result == pytest.approx(0.5)


# ── remove_vig ────────────────────────────────────────────────────────────────

def test_remove_vig_balanced_sums_to_one():
    a, b = remove_vig(-110, -110)
    assert a + b == pytest.approx(1.0, rel=1e-6)


def test_remove_vig_unbalanced_sums_to_one():
    a, b = remove_vig(-500, 400)
    assert a + b == pytest.approx(1.0, rel=1e-6)


def test_remove_vig_heavy_favorite_larger_prob():
    a, b = remove_vig(-500, 400)
    # -500 = heavy favorite, should have larger probability
    assert a > b


# ── compute_ev ────────────────────────────────────────────────────────────────

def test_compute_ev_certainty():
    # prob=1.0, decimal=2.0 → 1.0*(2-1) - 0 = 1.0
    assert compute_ev(1.0, 2.0) == pytest.approx(1.0)


def test_compute_ev_zero_chance():
    # prob=0.0, decimal=2.0 → 0 - 1.0 = -1.0
    assert compute_ev(0.0, 2.0) == pytest.approx(-1.0)


def test_compute_ev_breakeven():
    # prob=0.5, decimal=2.0 → 0.5*(1) - 0.5 = 0.0
    assert compute_ev(0.5, 2.0) == pytest.approx(0.0)


# ── assign_units ──────────────────────────────────────────────────────────────

def test_assign_units_below_minimum():
    assert assign_units(0.005) == 0


def test_assign_units_tier1_low_boundary():
    assert assign_units(0.01) == 1     # exactly at 1% boundary


def test_assign_units_tier1():
    assert assign_units(0.02) == 1


def test_assign_units_tier2_low_boundary():
    assert assign_units(0.03) == 2     # exactly at 3% boundary


def test_assign_units_tier2():
    assert assign_units(0.05) == 2


def test_assign_units_tier3_low_boundary():
    assert assign_units(0.06) == 3     # exactly at 6% boundary


def test_assign_units_tier3():
    assert assign_units(0.08) == 3


def test_assign_units_tier4_low_boundary():
    assert assign_units(0.10) == 4     # exactly at 10% boundary


def test_assign_units_tier4():
    assert assign_units(0.12) == 4


def test_assign_units_tier5_low_boundary():
    assert assign_units(0.15) == 5     # exactly at 15% boundary


def test_assign_units_tier5_high():
    assert assign_units(0.25) == 5


# ── bankroll_examples ─────────────────────────────────────────────────────────

def test_bankroll_examples_zero_units_returns_empty():
    result = bankroll_examples(0)
    assert result == {}


def test_bankroll_examples_negative_units_returns_empty():
    result = bankroll_examples(-1)
    assert result == {}


def test_bankroll_examples_positive_units_all_values():
    result = bankroll_examples(5, 0.01, -110)
    assert len(result) == 4
    for bankroll, vals in result.items():
        assert vals["stake"] > 0
        assert vals["potential_profit"] > 0
        assert vals["potential_return"] > 0


def test_bankroll_examples_correct_math():
    # bankroll=1000, units=5, unit_size=0.01, odds=-110 → stake=1000*0.01*5=50
    result = bankroll_examples(5, 0.01, -110)
    assert result[1000]["stake"] == pytest.approx(50.0)
    # dec = 100/110 + 1 ≈ 1.909
    dec = 100 / 110 + 1
    assert result[1000]["potential_profit"] == pytest.approx(50 * (dec - 1), rel=1e-3)


# ── evaluate ──────────────────────────────────────────────────────────────────

def test_evaluate_zero_odds_no_crash():
    result = evaluate(american_odds=0, projected_prob=0.55)
    assert result is not None
    assert result.ev_pct == 0.0
    assert result.is_positive_ev is False
    assert result.american_odds == 0


def test_evaluate_with_opponent_odds_uses_vig_removal():
    result = evaluate(american_odds=-110, projected_prob=0.55, opponent_odds=-110)
    # no_vig_prob should be 0.5 after vig removal (balanced market)
    assert result.no_vig_prob == pytest.approx(0.5, abs=0.01)


def test_evaluate_without_opponent_uses_book_implied():
    result = evaluate(american_odds=-110, projected_prob=0.55)
    # no_vig should match raw book implied prob
    assert result.no_vig_prob == pytest.approx(result.book_implied, rel=1e-6)


def test_evaluate_negative_projected_prob_clamped():
    result = evaluate(american_odds=200, projected_prob=-0.1)
    assert result.projected_prob == 0.0


def test_evaluate_positive_ev_detected():
    # proj=0.6, -110 → ev > 0
    result = evaluate(american_odds=-110, projected_prob=0.60)
    assert result.is_positive_ev is True
    assert result.ev_pct > 0
