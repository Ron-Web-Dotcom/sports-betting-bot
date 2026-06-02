"""Comprehensive CLV Engine tests."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.clv_engine import calculate_clv, aggregate_clv, clv_by_dimension


def test_clv_same_odds_is_zero():
    clv = calculate_clv(-110, -110)
    assert clv == pytest.approx(0.0, abs=1e-6)


def test_clv_beat_closing_line_positive():
    # Bet at -110, line moved to -130 (harder to bet) → positive CLV
    clv = calculate_clv(-110, -130)
    assert clv > 0.0


def test_clv_line_moved_against_us_negative():
    # Bet at -130, line moved to -110 → negative CLV
    clv = calculate_clv(-130, -110)
    assert clv < 0.0


def test_clv_formula_direction_we_got_better_price():
    # calculate_clv rounds to 4 decimal places — use abs tolerance
    from src.engines.ev_engine import implied_prob
    clv = calculate_clv(-110, -130)
    expected = implied_prob(-130) - implied_prob(-110)
    assert clv == pytest.approx(expected, abs=1e-3)


def test_clv_formula_direction_worse_price():
    from src.engines.ev_engine import implied_prob
    clv = calculate_clv(-130, -110)
    expected = implied_prob(-110) - implied_prob(-130)
    assert clv == pytest.approx(expected, abs=1e-3)


def test_aggregate_clv_no_records_returns_safe_default():
    # get_db is imported locally inside aggregate_clv — patch the source module
    with patch("src.db.session.get_db") as mock_get_db:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_get_db.return_value = mock_session

        result = aggregate_clv("daily")
        assert result["count"] == 0
        assert result["avg_clv"] == 0.0
        assert result["positive_pct"] == 0.0


def test_clv_by_dimension_accepts_period_arg():
    with patch("src.db.session.get_db") as mock_get_db:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_q = MagicMock()
        mock_q.filter.return_value.group_by.return_value.all.return_value = []
        mock_session.query.return_value = mock_q
        mock_get_db.return_value = mock_session

        result = clv_by_dimension("sport", "daily")
        assert isinstance(result, dict)


def test_clv_by_dimension_lifetime():
    with patch("src.db.session.get_db") as mock_get_db:
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_q = MagicMock()
        mock_q.filter.return_value.group_by.return_value.all.return_value = []
        mock_session.query.return_value = mock_q
        mock_get_db.return_value = mock_session

        result = clv_by_dimension("sport", "lifetime")
        assert isinstance(result, dict)
