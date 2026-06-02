import pytest
from src.engines.confidence_engine import compute_confidence, ConfidenceResult


def test_compute_confidence_returns_result():
    result = compute_confidence(
        ai_win_prob=0.7,
        model_consensus=0.6,
        line_movement_score=0.5,
        news_impact_score=0.4,
    )
    assert isinstance(result, ConfidenceResult)
    assert 0.0 <= result.raw_score <= 1.0
    assert 0.0 <= result.calibrated_score <= 1.0


def test_high_inputs_yield_high_confidence():
    result = compute_confidence(0.9, 0.9, 0.9, 0.9)
    assert result.calibrated_score > 0.7


def test_low_inputs_yield_low_confidence():
    result = compute_confidence(0.1, 0.1, 0.1, 0.1)
    assert result.calibrated_score < 0.4


def test_confidence_clipped_to_range():
    result = compute_confidence(1.0, 1.0, 1.0, 1.0)
    assert result.calibrated_score <= 1.0
    result2 = compute_confidence(0.0, 0.0, 0.0, 0.0)
    assert result2.calibrated_score >= 0.0
