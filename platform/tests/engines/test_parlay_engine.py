import pytest
from src.engines.parlay_engine import ParlayLeg, ParlayResult, build_parlay, find_best_parlays
from src.engines.ev_engine import american_to_decimal


def _leg(odds=-110, sport="basketball_nba", market="h2h", event_id="g1") -> ParlayLeg:
    from src.engines.ev_engine import implied_prob
    return ParlayLeg(
        event_id=event_id, event_name="Team A vs Team B",
        sport=sport, market=market, selection="Team A",
        book="DraftKings", american_odds=odds,
        win_probability=implied_prob(odds),
        ev_pct=0.04, confidence=0.65,
    )


def test_combined_decimal_two_legs():
    legs = [_leg(-110), _leg(-110)]
    result = build_parlay(legs)
    # -110/-110 parlay ≈ +260 combined
    assert result.combined_decimal > 3.0


def test_build_parlay_returns_result():
    legs = [_leg(-110, event_id="g1"), _leg(-110, event_id="g2")]
    result = build_parlay(legs)
    assert isinstance(result, ParlayResult)
    assert len(result.legs) == 2
    assert result.adjusted_ev is not None


def test_build_parlay_empty_zero_decimal():
    result = build_parlay([])
    assert result.combined_decimal == pytest.approx(1.0)


def test_find_best_parlays():
    legs = [_leg(-110, event_id=f"g{i}") for i in range(4)]
    parlays = find_best_parlays(legs, max_legs=3, top_n=2)
    assert len(parlays) <= 2


def test_correlation_risk_high_sgp():
    """Same-game parlay legs on same event should flag correlation risk."""
    legs = [_leg(-110, market="h2h", event_id="g1"),
            _leg(-110, market="totals", event_id="g1")]  # same event
    result = build_parlay(legs, parlay_type="sgp")
    assert result.correlation_risk in ("low", "medium", "high")
