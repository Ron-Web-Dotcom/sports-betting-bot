"""Tests for data_quality_engine."""
import pytest
from datetime import datetime, timezone, timedelta
from src.engines.data_quality_engine import (
    assess_event_quality,
    quality_to_confidence_adj,
    DataQualityReport,
)


def _good_event():
    return {
        "commence_time": datetime.utcnow() + timedelta(hours=2),
        "home_team": "Lakers",
        "away_team": "Celtics",
        "markets": [{"key": "h2h"}],
    }


def _fresh_snapshots(n_books=3):
    now = datetime.now(timezone.utc)
    return [
        {"book": f"book{i}", "market": "moneyline", "captured_at": now - timedelta(minutes=1)}
        for i in range(n_books)
    ]


def test_perfect_event_score():
    report = assess_event_quality(_good_event(), _fresh_snapshots(3), [])
    assert report.score > 0.9
    assert isinstance(report.issues, list)
    assert isinstance(report.missing_fields, list)


def test_missing_fields_penalizes():
    event = {"home_team": "Lakers"}  # missing 3 fields
    report = assess_event_quality(event, [], [])
    assert report.score < 0.8
    assert len(report.missing_fields) >= 3


def test_stale_data_penalizes():
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=20)
    snaps = [{"book": f"b{i}", "market": "spread", "captured_at": old_ts} for i in range(3)]
    report = assess_event_quality(_good_event(), snaps, [])
    assert report.score < 1.0
    assert any("old" in issue for issue in report.issues)


def test_few_books_penalizes():
    report = assess_event_quality(_good_event(), _fresh_snapshots(1), [])
    assert report.score < 1.0
    assert any("book" in issue for issue in report.issues)


def test_spread_conflict_flagged():
    now = datetime.now(timezone.utc)
    snaps = [
        {"book": "bookA", "market": "spread", "line_value": -3.0, "captured_at": now},
        {"book": "bookB", "market": "spread", "line_value": -7.5, "captured_at": now},
        {"book": "bookC", "market": "moneyline", "captured_at": now},
    ]
    report = assess_event_quality(_good_event(), snaps, [])
    assert len(report.source_conflicts) > 0


def test_quality_to_confidence_adj_bounds():
    assert quality_to_confidence_adj(0.0) == 0.5
    assert quality_to_confidence_adj(0.5) == 0.5
    assert quality_to_confidence_adj(1.0) == pytest.approx(1.0)


def test_quality_to_confidence_adj_midpoint():
    result = quality_to_confidence_adj(0.75)
    assert 0.7 < result < 1.0


def test_score_clamped_to_zero():
    event = {}  # all fields missing
    report = assess_event_quality(event, [], [])
    assert report.score >= 0.0
