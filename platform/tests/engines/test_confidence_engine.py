"""Comprehensive tests for confidence_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.confidence_engine import (
    compute_confidence, _bucket, get_calibration_adjustment, update_calibration,
    ConfidenceResult, WEIGHTS,
)


def test_bucket_ranges():
    assert _bucket(0.62) == "60-65"
    assert _bucket(0.65) == "65-70"
    assert _bucket(0.50) == "50-55"
    assert _bucket(0.0)  == "0-5"
    assert _bucket(0.99) == "95-100"


def test_compute_confidence_returns_result():
    result = compute_confidence(0.7, 0.6, 0.5, 0.4)
    assert isinstance(result, ConfidenceResult)
    assert 0.0 <= result.raw_score <= 1.0
    assert 0.0 <= result.calibrated_score <= 1.0


def test_high_inputs_yield_high_confidence():
    result = compute_confidence(0.9, 0.9, 0.9, 0.9)
    assert result.calibrated_score > 0.70


def test_low_inputs_yield_low_confidence():
    result = compute_confidence(0.1, 0.1, 0.1, 0.1)
    assert result.calibrated_score < 0.40


def test_confidence_clamped_high():
    result = compute_confidence(2.0, 2.0, 2.0, 2.0)
    assert result.raw_score <= 1.0
    assert result.calibrated_score <= 1.0


def test_confidence_clamped_low():
    result = compute_confidence(-1.0, -1.0, -1.0, -1.0)
    assert result.raw_score >= 0.0
    assert result.calibrated_score >= 0.0


def test_weights_sum_correct():
    non_cal = WEIGHTS["ai_prob"] + WEIGHTS["model_consensus"] + WEIGHTS["line_movement"] + WEIGHTS["news_impact"]
    assert abs(non_cal + WEIGHTS["calibration"] - 1.0) < 1e-9


def test_all_fields_present():
    result = compute_confidence(0.65, 0.70, 0.60, 0.50)
    for field in ("raw_score", "calibrated_score", "ai_prob", "model_consensus",
                  "line_movement", "news_impact", "calibration_adj", "confidence_bucket"):
        assert hasattr(result, field)


def test_bucket_matches_raw():
    result = compute_confidence(0.70, 0.70, 0.70, 0.70)
    low = int(result.raw_score * 100 // 5) * 5
    assert result.confidence_bucket == f"{low}-{low+5}"


def test_calibration_adjustment_zero_on_db_error():
    with patch("src.db.session.get_db", side_effect=Exception("DB down")):
        adj = get_calibration_adjustment("nba", "h2h", "60-65")
    assert adj == 0.0


def test_calibration_adjustment_zero_no_record():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter_by.return_value.first.return_value = None
    with patch("src.db.session.get_db", return_value=ms):
        assert get_calibration_adjustment("nba", "h2h", "60-65") == 0.0


def test_calibration_adjustment_zero_insufficient_sample():
    rec = MagicMock(actual_win_rate=0.70, predicted_win_rate=0.63, sample_size=5)
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter_by.return_value.first.return_value = rec
    with patch("src.db.session.get_db", return_value=ms):
        assert get_calibration_adjustment("nba", "h2h", "60-65") == 0.0


def test_calibration_adjustment_positive_when_outperforming():
    rec = MagicMock(actual_win_rate=0.70, predicted_win_rate=0.63, sample_size=50)
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter_by.return_value.first.return_value = rec
    with patch("src.db.session.get_db", return_value=ms):
        adj = get_calibration_adjustment("nba", "h2h", "60-65")
    assert adj == pytest.approx(0.07, abs=0.001)


def test_update_calibration_creates_record():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter_by.return_value.first.return_value = None
    with patch("src.db.session.get_db", return_value=ms):
        update_calibration("nba", "h2h", "60-65", won=True)
    ms.add.assert_called_once()


def test_update_calibration_updates_win_rate():
    rec = MagicMock(actual_win_rate=0.60, sample_size=10, predicted_win_rate=0.625)
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter_by.return_value.first.return_value = rec
    with patch("src.db.session.get_db", return_value=ms):
        update_calibration("nba", "h2h", "60-65", won=True)
    assert rec.actual_win_rate == pytest.approx((0.60 * 10 + 1.0) / 11, abs=0.001)
    assert rec.sample_size == 11


def test_update_calibration_silent_on_db_error():
    with patch("src.db.session.get_db", side_effect=Exception("DB down")):
        update_calibration("nba", "h2h", "60-65", won=False)  # must not raise
