"""Tests for odds_engine."""
import pytest
from unittest.mock import patch, MagicMock
from src.engines.odds_engine import (
    normalise_event, best_odds, fetch_events, fetch_scores, scan_all_sports,
)


RAW_EVENT = {
    "id": "abc123",
    "home_team": "Lakers",
    "away_team": "Celtics",
    "commence_time": "2026-06-02T20:00:00Z",
    "bookmakers": [
        {
            "key": "draftkings",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Lakers", "price": -120},
                        {"name": "Celtics", "price": 100},
                    ],
                },
                {
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Lakers", "price": -110, "point": -2.5},
                        {"name": "Celtics", "price": -110, "point": 2.5},
                    ],
                },
            ],
        },
        {
            "key": "fanduel",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Lakers", "price": -115},
                        {"name": "Celtics", "price": 105},
                    ],
                },
            ],
        },
    ],
}


def test_normalise_event_structure():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    assert norm["id"] == "abc123"
    assert norm["home_team"] == "Lakers"
    assert norm["away_team"] == "Celtics"
    assert norm["sport"] == "basketball_nba"
    assert "markets" in norm


def test_normalise_event_markets():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    assert "h2h" in norm["markets"]
    assert "spreads" in norm["markets"]
    assert "Lakers" in norm["markets"]["h2h"]
    assert "Celtics" in norm["markets"]["h2h"]


def test_normalise_event_best_odds_first():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    lakers_h2h = norm["markets"]["h2h"]["Lakers"]
    # -115 (fanduel) has better decimal odds for favourite than -120 (dk)
    # Sorted descending by decimal_odds
    assert lakers_h2h[0]["decimal_odds"] >= lakers_h2h[1]["decimal_odds"]


def test_normalise_event_includes_decimal_and_prob():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    entry = norm["markets"]["h2h"]["Lakers"][0]
    assert "decimal_odds" in entry
    assert "implied_prob" in entry
    assert 0 < entry["implied_prob"] < 1


def test_normalise_event_spreads_have_line():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    spread_entry = norm["markets"]["spreads"]["Lakers"][0]
    assert "line" in spread_entry
    assert spread_entry["line"] == -2.5


def test_best_odds_returns_top_entry():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    result = best_odds(norm, "h2h", "Lakers")
    assert result is not None
    assert result["decimal_odds"] == max(
        e["decimal_odds"] for e in norm["markets"]["h2h"]["Lakers"]
    )


def test_best_odds_missing_market_returns_none():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    assert best_odds(norm, "totals", "Over 220") is None


def test_best_odds_missing_selection_returns_none():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    assert best_odds(norm, "h2h", "Unknown Team") is None


def test_fetch_events_returns_empty_on_api_error():
    with patch("src.engines.odds_engine._get", return_value=None):
        result = fetch_events("basketball_nba")
    assert result == []


def test_fetch_scores_returns_empty_on_api_error():
    with patch("src.engines.odds_engine._get", return_value=None):
        result = fetch_scores("basketball_nba")
    assert result == []


def test_fetch_scores_returns_list():
    with patch("src.engines.odds_engine._get", return_value=[{"id": "x"}]):
        result = fetch_scores("basketball_nba")
    assert result == [{"id": "x"}]


def test_scan_all_sports_empty_on_api_failure():
    with patch("src.engines.odds_engine.fetch_events", return_value=[]):
        result = scan_all_sports()
    assert isinstance(result, dict)
    # All sports returned empty — no sport should be in result
    assert all(len(v) == 0 for v in result.values()) or result == {}


def test_scan_all_sports_normalises_events():
    with patch("src.engines.odds_engine.fetch_events", return_value=[RAW_EVENT]):
        result = scan_all_sports()
    for sport_key, events in result.items():
        for ev in events:
            assert "markets" in ev
            assert "id" in ev


def test_normalise_event_name_format():
    norm = normalise_event(RAW_EVENT, "basketball_nba")
    assert "Celtics" in norm["name"]
    assert "Lakers" in norm["name"]
