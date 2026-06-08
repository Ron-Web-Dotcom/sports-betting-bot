import pytest
from src.engines.ev_engine import (
    american_to_decimal,
    decimal_to_american,
    implied_prob,
    remove_vig,
    compute_ev,
    assign_units,
    bankroll_examples,
)


def test_american_to_decimal_positive():
    assert american_to_decimal(200) == pytest.approx(3.0)


def test_american_to_decimal_negative():
    assert american_to_decimal(-110) == pytest.approx(1.909, rel=1e-3)


def test_decimal_to_american_positive():
    assert decimal_to_american(3.0) == 200


def test_decimal_to_american_negative():
    assert decimal_to_american(1.909) == pytest.approx(-110, abs=2)


def test_implied_prob_positive_odds():
    prob = implied_prob(200)
    assert prob == pytest.approx(1 / 3, rel=1e-3)


def test_implied_prob_negative_odds():
    prob = implied_prob(-110)
    assert 0.52 < prob < 0.53


def test_remove_vig_two_sided():
    fair_a, fair_b = remove_vig(-110, -110)
    assert fair_a + fair_b == pytest.approx(1.0, rel=1e-3)
    assert fair_a == pytest.approx(0.5, abs=0.01)


def test_compute_ev_positive():
    ev = compute_ev(projected_prob=0.55, decimal_odds=2.0)
    assert ev > 0


def test_compute_ev_negative():
    ev = compute_ev(projected_prob=0.40, decimal_odds=2.0)
    assert ev < 0


def test_assign_units_thresholds():
    assert assign_units(0.005) == 0   # below 1%
    assert assign_units(0.02)  == 1   # 1-3%
    assert assign_units(0.05)  == 2   # 3-6%
    assert assign_units(0.08)  == 3   # 6-10%
    assert assign_units(0.12)  == 4   # 10-15%
    assert assign_units(0.20)  == 5   # 15%+


def test_bankroll_examples_keys():
    examples = bankroll_examples(units=2, unit_size_pct=0.01, american_odds=-110)
    assert set(examples.keys()) == {100, 500, 1000, 5000}
    for v in examples.values():
        assert "stake" in v
        assert "potential_profit" in v
        assert "potential_return" in v
