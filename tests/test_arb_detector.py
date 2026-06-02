"""Tests for arbitrage detection."""
import pytest
from src.core.arb_detector import _arb_profit, _optimal_stakes, scan_event_for_arbs


class TestArbMath:
    def test_no_arb_at_vig(self):
        # Two -110 sides: sum of implied probs > 1 = no arb
        from src.apis.odds_api import american_to_decimal
        dec = american_to_decimal(-110)
        assert _arb_profit(dec, dec) == 0.0

    def test_arb_exists(self):
        # +130 / -110 across books
        from src.apis.odds_api import american_to_decimal
        dec_a = american_to_decimal(130)
        dec_b = american_to_decimal(-105)
        profit = _arb_profit(dec_a, dec_b)
        assert profit > 0.005

    def test_optimal_stakes_sum_to_total(self):
        from src.apis.odds_api import american_to_decimal
        dec_a = american_to_decimal(120)
        dec_b = american_to_decimal(-115)
        s_a, s_b = _optimal_stakes(dec_a, dec_b, 100.0)
        assert abs(s_a + s_b - 100.0) < 0.05

    def test_optimal_stakes_equal_payout(self):
        from src.apis.odds_api import american_to_decimal
        dec_a = american_to_decimal(120)
        dec_b = american_to_decimal(-115)
        s_a, s_b = _optimal_stakes(dec_a, dec_b, 100.0)
        payout_a = s_a * dec_a
        payout_b = s_b * dec_b
        assert abs(payout_a - payout_b) < 1.0


class TestScanEvent:
    def _make_event(self, odds_a: int, odds_b: int, same_book: bool = False) -> dict:
        book_b = "draftkings" if same_book else "fanduel"
        return {
            "id": "evt1",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Lakers",  "price": odds_a},
                        {"name": "Celtics", "price": -120},
                    ]}],
                },
                {
                    "key": book_b,
                    "markets": [{"key": "h2h", "outcomes": [
                        {"name": "Lakers",  "price": -130},
                        {"name": "Celtics", "price": odds_b},
                    ]}],
                },
            ],
        }

    def test_finds_arb_across_books(self):
        # Lakers +130 at DK, Celtics +120 at FD — arb exists
        event = self._make_event(130, 120)
        arbs = scan_event_for_arbs(event)
        assert len(arbs) > 0
        assert arbs[0].guaranteed_profit_pct > 0

    def test_no_arb_same_book_skipped(self):
        event = self._make_event(130, 120, same_book=True)
        arbs = scan_event_for_arbs(event)
        # Same book arbs are filtered out
        assert all(a.book_a != a.book_b for a in arbs)

    def test_no_arb_normal_market(self):
        event = self._make_event(-110, -110)
        arbs = scan_event_for_arbs(event)
        # Standard -110/-110 has no arb
        assert len(arbs) == 0
