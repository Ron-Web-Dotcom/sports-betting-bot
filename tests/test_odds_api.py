"""Tests for odds utilities and API helpers."""
import pytest
from src.apis.odds_api import american_to_decimal, implied_prob


class TestAmericanToDecimal:
    def test_positive_odds(self):
        assert american_to_decimal(100)  == pytest.approx(2.00)
        assert american_to_decimal(150)  == pytest.approx(2.50)
        assert american_to_decimal(200)  == pytest.approx(3.00)

    def test_negative_odds(self):
        assert american_to_decimal(-110) == pytest.approx(1.909, abs=0.001)
        assert american_to_decimal(-150) == pytest.approx(1.667, abs=0.001)
        assert american_to_decimal(-200) == pytest.approx(1.500)


class TestImpliedProb:
    def test_even_money(self):
        assert implied_prob(100) == pytest.approx(0.50)

    def test_heavy_favourite(self):
        assert implied_prob(-200) == pytest.approx(0.667, abs=0.001)

    def test_underdog(self):
        assert implied_prob(200) == pytest.approx(0.333, abs=0.001)

    def test_vig_baked_in(self):
        # Two-sided market has total implied prob > 1 (vig)
        home = implied_prob(-110)
        away = implied_prob(-110)
        assert home + away > 1.0
