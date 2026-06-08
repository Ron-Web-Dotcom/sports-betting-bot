"""Comprehensive tests for line_movement_engine — from 0% coverage."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from src.engines.line_movement_engine import (
    detect_movements, _classify_movement, score_for_confidence,
    save_movement, LineMovementAlert,
)


def _snap(book="dk", american_odds=-110, minutes_ago=0, market="h2h", selection="TeamA"):
    return {
        "book": book,
        "market": market,
        "selection": selection,
        "american_odds": american_odds,
        "captured_at": datetime.utcnow() - timedelta(minutes=minutes_ago),
    }


# ── detect_movements ──────────────────────────────────────────────────────────

def test_detect_movements_empty_returns_empty():
    assert detect_movements(1, "A vs B", []) == []


def test_detect_movements_single_snap_no_movement():
    snaps = [_snap()]
    assert detect_movements(1, "A vs B", snaps) == []


def test_detect_movements_identical_odds_no_alert():
    snaps = [_snap(minutes_ago=20), _snap(minutes_ago=0)]
    result = detect_movements(1, "A vs B", snaps)
    assert result == []


def test_detect_movements_large_shift_triggers_alert():
    # -110 → +150 is a massive shift
    snaps = [
        _snap(american_odds=-110, minutes_ago=25),
        _snap(american_odds=+150, minutes_ago=0),
    ]
    result = detect_movements(1, "A vs B", snaps)
    assert len(result) > 0
    assert isinstance(result[0], LineMovementAlert)


def test_detect_movements_alert_has_correct_fields():
    snaps = [
        _snap(american_odds=-110, minutes_ago=25),
        _snap(american_odds=-130, minutes_ago=0),
    ]
    result = detect_movements(1, "A vs B", snaps)
    assert len(result) > 0
    alert = result[0]
    assert alert.game_id == 1
    assert alert.event_name == "A vs B"
    assert alert.odds_before == -110
    assert alert.odds_after == -130
    assert alert.delta_pct > 0


def test_detect_movements_groups_by_book():
    # Two books — movements tracked per book separately
    snaps = [
        _snap(book="dk", american_odds=-110, minutes_ago=25),
        _snap(book="dk", american_odds=-130, minutes_ago=0),
        _snap(book="fd", american_odds=-110, minutes_ago=25),
        _snap(book="fd", american_odds=-110, minutes_ago=0),
    ]
    result = detect_movements(1, "A vs B", snaps)
    # Only dk moved; fd stayed the same
    assert len(result) == 1
    assert result[0].book == "dk"


def test_detect_movements_returns_list_of_alerts():
    snaps = [
        _snap(american_odds=-110, minutes_ago=25),
        _snap(american_odds=+120, minutes_ago=0),
    ]
    result = detect_movements(1, "Test", snaps)
    assert all(isinstance(a, LineMovementAlert) for a in result)


# ── _classify_movement ────────────────────────────────────────────────────────

def test_classify_steam_large_rapid_move():
    # Large positive delta (prob shortened quickly) with few snaps = steam
    snaps = [{"captured_at": datetime.utcnow() - timedelta(minutes=5)},
             {"captured_at": datetime.utcnow()}]
    mv = _classify_movement(0.05, snaps)
    assert mv == "steam"


def test_classify_sharp_improving_odds():
    # Negative delta = odds improving for this side = sharp money
    snaps = [{}] * 5
    mv = _classify_movement(-0.04, snaps)
    assert mv == "sharp"


def test_classify_public_shortening_odds():
    # Positive delta = odds shortening = public money
    snaps = [{}] * 5
    mv = _classify_movement(0.02, snaps)
    assert mv == "public"


def test_classify_reverse_line_movement():
    snaps = [{}] * 5
    mv = _classify_movement(-0.01, snaps)
    assert mv == "reverse"


# ── score_for_confidence ──────────────────────────────────────────────────────

def test_score_returns_neutral_when_no_data():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = []
    ms.query.return_value.filter.return_value.all.return_value = []
    with patch("src.db.session.get_db", return_value=ms):
        score = score_for_confidence("g1", "h2h", "TeamA")
    assert score == 0.5


def test_score_returns_neutral_on_db_error():
    with patch("src.db.session.get_db", side_effect=Exception("DB down")):
        score = score_for_confidence("g1", "h2h", "TeamA")
    assert score == 0.5


def test_score_above_neutral_with_sharp_moves():
    sharp_move = MagicMock(movement_type="sharp")
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    # score_for_confidence uses a single .filter(...) call with multiple conditions
    ms.query.return_value.filter.return_value.all.return_value = [sharp_move, sharp_move]
    with patch("src.db.session.get_db", return_value=ms):
        score = score_for_confidence("g1", "h2h", "TeamA")
    assert score > 0.5


def test_score_clamped_between_0_and_1():
    with patch("src.db.session.get_db", side_effect=Exception):
        score = score_for_confidence("g1", "h2h", "TeamA")
    assert 0.0 <= score <= 1.0


# ── save_movement ─────────────────────────────────────────────────────────────

def test_save_movement_writes_to_db():
    alert = LineMovementAlert(
        game_id=1, event_name="A vs B", market="h2h", selection="TeamA",
        book="dk", odds_before=-110, odds_after=-130, delta_pct=0.05,
        movement_type="sharp", detected_at=datetime.utcnow(),
    )
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    with patch("src.db.session.get_db", return_value=ms):
        save_movement(alert)
    ms.add.assert_called_once()


def test_save_movement_handles_string_game_id():
    alert = LineMovementAlert(
        game_id="not-a-number", event_name="A vs B", market="h2h",
        selection="TeamA", book="dk", odds_before=-110, odds_after=-130,
        delta_pct=0.05, movement_type="steam", detected_at=datetime.utcnow(),
    )
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    with patch("src.db.session.get_db", return_value=ms):
        save_movement(alert)  # must not raise even with non-numeric game_id
    ms.add.assert_called_once()
