"""Tests for hedge calculation math."""
import pytest
from src.core.hedge_engine import calculate_hedge


def test_full_hedge_guarantees_profit():
    rec = calculate_hedge(
        parlay_id=1,
        parlay_stake=10.0,
        potential_payout=100.0,
        hedge_american_odds=-110,
        hedge_selection="Opponent",
        hedge_book="fanduel",
        legs_remaining=1,
        trigger_summary="3/4 legs won",
    )
    assert rec.full_hedge_profit > 0
    # Verify: full_hedge_stake * decimal_odds ≈ potential_payout
    from src.apis.odds_api import american_to_decimal
    dec = american_to_decimal(-110)
    assert abs(rec.full_hedge_stake * dec - 100.0) < 1.0


def test_partial_hedge_upside_greater_than_full():
    rec = calculate_hedge(
        parlay_id=2,
        parlay_stake=10.0,
        potential_payout=80.0,
        hedge_american_odds=100,
        hedge_selection="Opponent",
        hedge_book="draftkings",
        legs_remaining=1,
        trigger_summary="2/3 legs won",
    )
    # Partial hedge keeps upside but floors downside
    assert rec.partial_hedge_upside > rec.full_hedge_profit
    assert rec.partial_hedge_stake < rec.full_hedge_stake


def test_hedge_fields_populated():
    rec = calculate_hedge(1, 5.0, 50.0, -120, "Team B", "betmgm", 1, "test")
    assert rec.parlay_id == 1
    assert rec.potential_payout == 50.0
    assert rec.full_hedge_stake > 0
    assert rec.partial_hedge_stake == pytest.approx(rec.full_hedge_stake * 0.5, rel=0.01)
