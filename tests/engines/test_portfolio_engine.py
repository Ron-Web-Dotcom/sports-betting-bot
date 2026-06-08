import pytest
from src.engines.portfolio_engine import build_daily_portfolio, DailyPortfolio


def _pick(units: int, sport: str = "NBA", ev: float = 0.05, score: float = 0.7) -> dict:
    return {
        "id": 1, "bet": "Team ML", "sport": sport,
        "units": units, "ev_pct": ev, "opportunity_score": score,
    }


def test_build_portfolio_categorises_picks():
    # Use many different sports + low units to avoid concentration limits
    picks = [_pick(1, sport="NBA"), _pick(2, sport="NFL"), _pick(3, sport="MLB")]
    portfolio = build_daily_portfolio(picks)
    assert isinstance(portfolio, DailyPortfolio)
    # Each is accepted: total=6u, each sport is only 1 pick at a time
    total = len(portfolio.safe_picks) + len(portfolio.medium_picks) + len(portfolio.high_picks)
    assert total >= 1  # at least one pick accepted


def test_portfolio_respects_daily_unit_limit():
    from src.core.config import MAX_DAILY_UNITS
    picks = [_pick(5, sport=f"SPORT{i}") for i in range(20)]  # 100u total, different sports
    portfolio = build_daily_portfolio(picks)
    assert portfolio.total_units <= MAX_DAILY_UNITS


def test_portfolio_summary_string():
    picks = [_pick(2), _pick(3)]
    portfolio = build_daily_portfolio(picks)
    summary = portfolio.summary()
    assert "Portfolio" in summary
    assert "total" in summary


def test_empty_picks_empty_portfolio():
    portfolio = build_daily_portfolio([])
    assert portfolio.total_units == 0
    assert portfolio.expected_roi == 0.0
