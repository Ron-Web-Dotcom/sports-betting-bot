"""Tests for parlay construction and optimization."""
import pytest
from unittest.mock import patch, MagicMock
from src.core.signal_engine import Signal
from src.core.parlay_builder import build_standard_parlays, _combined_decimal, _are_correlated


def _make_signal(event_id: str, sport: str = "basketball_nba",
                 confidence: float = 0.68, win_prob: float = 0.65,
                 american_odds: int = -110, composite: float = 0.67) -> Signal:
    sig = Signal(
        sport=sport, event_id=event_id, event_name=f"Game {event_id}",
        market="h2h", selection=f"Team_{event_id}",
        book="draftkings", american_odds=american_odds,
        ai_confidence=confidence, win_probability=win_prob,
        composite_score=composite, edge=0.05,
    )
    return sig


class TestCombinedDecimal:
    def test_two_legs(self):
        s1 = _make_signal("a", american_odds=100)  # 2.0 decimal
        s2 = _make_signal("b", american_odds=100)
        assert _combined_decimal([s1, s2]) == pytest.approx(4.0)

    def test_three_legs(self):
        legs = [_make_signal(str(i), american_odds=100) for i in range(3)]
        assert _combined_decimal(legs) == pytest.approx(8.0)


class TestCorrelation:
    def test_same_event_correlated(self):
        s1 = _make_signal("evt1")
        s2 = _make_signal("evt1")
        assert _are_correlated(s1, s2) is True

    def test_different_events_uncorrelated(self):
        s1 = _make_signal("evt1")
        s2 = _make_signal("evt2")
        assert _are_correlated(s1, s2) is False


class TestBuildParlays:
    @patch("src.core.parlay_builder.size_parlay")
    def test_builds_parlays_from_signals(self, mock_size):
        mock_size.return_value = {
            "approved": True, "stake": 10.0, "kelly_fraction": 0.02,
            "edge": 0.05, "win_probability": 0.4,
        }
        signals = [_make_signal(str(i)) for i in range(4)]
        candidates = build_standard_parlays(signals)
        assert len(candidates) > 0

    @patch("src.core.parlay_builder.size_parlay")
    def test_excludes_same_game_legs(self, mock_size):
        mock_size.return_value = {
            "approved": True, "stake": 10.0, "kelly_fraction": 0.02,
            "edge": 0.05, "win_probability": 0.4,
        }
        # Two signals from same game + two from different games
        signals = [
            _make_signal("same"),
            _make_signal("same"),
            _make_signal("diff1"),
            _make_signal("diff2"),
        ]
        candidates = build_standard_parlays(signals)
        for c in candidates:
            event_ids = [s.event_id for s in c.legs]
            assert len(event_ids) == len(set(event_ids)), "Correlated legs found in parlay"

    @patch("src.core.parlay_builder.size_parlay")
    def test_sorted_by_ev(self, mock_size):
        mock_size.return_value = {
            "approved": True, "stake": 10.0, "kelly_fraction": 0.02,
            "edge": 0.05, "win_probability": 0.4,
        }
        signals = [_make_signal(str(i)) for i in range(5)]
        candidates = build_standard_parlays(signals)
        evs = [c.ev for c in candidates]
        assert evs == sorted(evs, reverse=True)
