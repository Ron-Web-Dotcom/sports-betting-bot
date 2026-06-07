"""Tests for personalization_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.personalization_engine import (
    UserProfile, get_profile, save_profile, apply_profile, RISK_PROFILES,
)


def test_risk_profiles_defined():
    for profile in ("conservative", "balanced", "aggressive"):
        assert profile in RISK_PROFILES
        assert "max_units" in RISK_PROFILES[profile]
        assert "min_ev" in RISK_PROFILES[profile]


def test_default_user_profile():
    profile = UserProfile(user_id="test")
    assert profile.bankroll == 1000.0
    assert profile.risk_profile == "balanced"
    assert profile.max_units == 3
    assert profile.min_ev == 0.02


def test_get_profile_returns_default_when_not_found():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = None
    with patch("src.db.session.get_db", return_value=ms):
        profile = get_profile("new_user")
    assert profile.user_id == "new_user"
    assert profile.risk_profile == "balanced"


def test_get_profile_returns_existing():
    row = MagicMock()
    row.user_id = "existing"
    row.bankroll = 5000.0
    row.risk_profile = "aggressive"
    row.sports = ["nba", "nfl"]
    row.max_units = 5
    row.min_ev = 0.01
    row.created_at = None
    row.updated_at = None

    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = row

    with patch("src.db.session.get_db", return_value=ms):
        profile = get_profile("existing")

    assert profile.bankroll == 5000.0
    assert profile.risk_profile == "aggressive"
    assert "nba" in profile.sports


def test_get_profile_parses_json_sports():
    row = MagicMock()
    row.user_id = "u"
    row.bankroll = 1000.0
    row.risk_profile = "balanced"
    row.sports = '["nba", "mlb"]'  # JSON string from old rows
    row.max_units = 3
    row.min_ev = 0.02
    row.created_at = None
    row.updated_at = None

    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = row

    with patch("src.db.session.get_db", return_value=ms):
        profile = get_profile("u")

    assert profile.sports == ["nba", "mlb"]


def test_save_profile_creates_new():
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = None

    profile = UserProfile(user_id="newuser", bankroll=2000.0)
    with patch("src.db.session.get_db", return_value=ms):
        save_profile(profile)

    ms.add.assert_called_once()


def test_save_profile_updates_existing():
    row = MagicMock()
    ms = MagicMock()
    ms.__enter__ = MagicMock(return_value=ms)
    ms.__exit__ = MagicMock(return_value=False)
    ms.query.return_value.filter.return_value.first.return_value = row

    profile = UserProfile(user_id="existing", bankroll=9999.0, risk_profile="conservative")
    with patch("src.db.session.get_db", return_value=ms):
        save_profile(profile)

    assert row.bankroll == 9999.0
    assert row.risk_profile == "conservative"
    ms.add.assert_not_called()


def test_apply_profile_filters_by_sport():
    picks = [
        {"sport": "nba", "ev_pct": 0.05, "units": 2},
        {"sport": "nfl", "ev_pct": 0.05, "units": 2},
    ]
    profile = UserProfile(user_id="u", sports=["nba"])
    result = apply_profile(picks, profile)
    assert len(result) == 1
    assert result[0]["sport"] == "nba"


def test_apply_profile_no_sport_filter():
    picks = [
        {"sport": "nba", "ev_pct": 0.05, "units": 2},
        {"sport": "nfl", "ev_pct": 0.05, "units": 2},
    ]
    profile = UserProfile(user_id="u", sports=[])
    result = apply_profile(picks, profile)
    assert len(result) == 2


def test_apply_profile_filters_by_min_ev():
    picks = [
        {"sport": "nba", "ev_pct": 0.01, "units": 2},
        {"sport": "nba", "ev_pct": 0.05, "units": 2},
    ]
    profile = UserProfile(user_id="u", min_ev=0.03)
    result = apply_profile(picks, profile)
    assert len(result) == 1
    assert result[0]["ev_pct"] == 0.05


def test_apply_profile_caps_units():
    picks = [{"sport": "nba", "ev_pct": 0.05, "units": 5}]
    profile = UserProfile(user_id="u", max_units=2)
    result = apply_profile(picks, profile)
    assert result[0]["units"] == 2


def test_apply_profile_does_not_exceed_max_units():
    picks = [{"sport": "nba", "ev_pct": 0.05, "units": 3}]
    profile = UserProfile(user_id="u", max_units=5)
    result = apply_profile(picks, profile)
    assert result[0]["units"] == 3  # not changed — already within limit
