"""Tests for feedback_engine."""
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime
from src.engines.feedback_engine import (
    PickFeedback,
    submit_feedback,
    get_pick_quality_score,
    get_explanation_quality,
    get_overall_satisfaction,
    apply_feedback_to_model,
)


def _feedback_row(rating="helpful", explanation_rating=4, submitted_at=None):
    row = MagicMock()
    row.rating = rating
    row.explanation_rating = explanation_rating
    row.submitted_at = submitted_at or datetime.utcnow()
    return row


def _make_ctx(rows=None):
    ctx = MagicMock()
    db = MagicMock()
    if rows is not None:
        db.query.return_value.filter.return_value.all.return_value = rows
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, db


@patch("src.db.session.get_db")
def test_submit_feedback_adds_row(mock_get_db):
    ctx, db = _make_ctx()
    mock_get_db.return_value = ctx

    fb = PickFeedback(pick_id=1, user_id="user1", rating="helpful", explanation_rating=5)
    submit_feedback(fb)
    db.add.assert_called_once()


@patch("src.db.session.get_db")
def test_quality_score_all_helpful(mock_get_db):
    rows = [_feedback_row("helpful"), _feedback_row("helpful")]
    ctx, _ = _make_ctx(rows)
    mock_get_db.return_value = ctx
    score = get_pick_quality_score(1)
    assert score == pytest.approx(1.0)


@patch("src.db.session.get_db")
def test_quality_score_mixed(mock_get_db):
    rows = [_feedback_row("helpful"), _feedback_row("not_helpful")]
    ctx, _ = _make_ctx(rows)
    mock_get_db.return_value = ctx
    score = get_pick_quality_score(1)
    assert score == pytest.approx(0.5)


@patch("src.db.session.get_db")
def test_quality_score_empty(mock_get_db):
    ctx, _ = _make_ctx([])
    mock_get_db.return_value = ctx
    assert get_pick_quality_score(1) == 0.0


@patch("src.db.session.get_db")
def test_explanation_quality(mock_get_db):
    rows = [_feedback_row(explanation_rating=4), _feedback_row(explanation_rating=2)]
    ctx, _ = _make_ctx(rows)
    mock_get_db.return_value = ctx
    avg = get_explanation_quality(1)
    assert avg == pytest.approx(3.0)


@patch("src.db.session.get_db")
def test_overall_satisfaction(mock_get_db):
    rows = [_feedback_row("helpful", 5), _feedback_row("helpful", 3), _feedback_row("not_helpful", 2)]
    ctx, _ = _make_ctx(rows)
    mock_get_db.return_value = ctx
    result = get_overall_satisfaction("weekly")
    assert "satisfaction_rate" in result
    assert "total_ratings" in result
    assert "avg_explanation" in result
    assert result["total_ratings"] == 3
    assert result["satisfaction_rate"] == pytest.approx(2/3)


@patch("src.db.session.get_db")
def test_overall_satisfaction_empty(mock_get_db):
    ctx, _ = _make_ctx([])
    mock_get_db.return_value = ctx
    result = get_overall_satisfaction()
    assert result["satisfaction_rate"] == 0.0
    assert result["total_ratings"] == 0


@patch("src.db.session.get_db")
def test_apply_feedback_no_history(mock_get_db):
    ctx = MagicMock()
    db = MagicMock()
    db.query.return_value.all.return_value = []
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    mock_get_db.return_value = ctx

    picks = [{"sport": "nba", "market": "moneyline", "ev_pct": 0.05}]
    result = apply_feedback_to_model(picks)
    assert len(result) == 1
    assert result[0]["ev_pct"] == pytest.approx(0.05)
