import pytest
from src.engines.clv_engine import calculate_clv


def test_clv_positive_when_open_better():
    """Opened at +200, closed at +150 — we got a better price."""
    clv = calculate_clv(200, 150)
    assert clv > 0


def test_clv_negative_when_closed_better():
    """Opened at -120, closed at -105 — line moved against us."""
    clv = calculate_clv(-120, -105)
    assert clv < 0


def test_clv_zero_when_same():
    clv = calculate_clv(-110, -110)
    assert clv == pytest.approx(0.0, abs=0.001)


def test_clv_returns_float():
    clv = calculate_clv(100, 110)
    assert isinstance(clv, float)
