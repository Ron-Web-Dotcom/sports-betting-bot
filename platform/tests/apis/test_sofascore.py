"""Tests for SofaScore adapter."""
import pytest
from unittest.mock import patch


# ── Helpers ────────────────────────────────────────────────────────────────────

def _event(home="Lakers", away="Celtics", event_id=12345, ts=1700000000):
    return {
        "id": event_id,
        "homeTeam": {"id": 1, "name": home},
        "awayTeam": {"id": 2, "name": away},
        "homeScore": {"current": 110},
        "awayScore": {"current": 105},
        "status": {"description": "Finished", "type": "finished"},
        "startTimestamp": ts,
        "tournament": {"name": "NBA"},
        "season": {"name": "2024/2025"},
    }


# ── get_scheduled_events ───────────────────────────────────────────────────────

def test_get_scheduled_events_returns_normalised_list():
    from src.apis.sofascore import get_scheduled_events
    with patch("src.apis.sofascore.get_json", return_value={"events": [_event()]}):
        result = get_scheduled_events("basketball_nba", "2024-11-15")
    assert len(result) == 1
    ev = result[0]
    assert ev["home_team"] == "Lakers"
    assert ev["away_team"] == "Celtics"
    assert ev["source"] == "sofascore"
    assert ev["home_score"] == 110


def test_get_scheduled_events_unknown_sport_returns_empty():
    from src.apis.sofascore import get_scheduled_events
    result = get_scheduled_events("unknown_sport_xyz", "2024-11-15")
    assert result == []


def test_get_scheduled_events_empty_response():
    from src.apis.sofascore import get_scheduled_events
    with patch("src.apis.sofascore.get_json", return_value=None):
        result = get_scheduled_events("basketball_nba", "2024-11-15")
    assert result == []


# ── get_h2h ────────────────────────────────────────────────────────────────────

def test_get_h2h_returns_results_with_winner():
    from src.apis.sofascore import get_h2h
    payload = {"events": [
        _event(home="Lakers", away="Celtics"),
        _event(home="Celtics", away="Lakers"),
    ]}
    with patch("src.apis.sofascore.get_json", return_value=payload):
        result = get_h2h("12345")
    assert len(result) == 2
    assert result[0]["winner"] == "home"   # 110 > 105
    assert result[0]["source"] == "sofascore"


def test_get_h2h_draw():
    from src.apis.sofascore import get_h2h
    ev = _event()
    ev["homeScore"]["current"] = 100
    ev["awayScore"]["current"] = 100
    with patch("src.apis.sofascore.get_json", return_value={"events": [ev]}):
        result = get_h2h("1")
    assert result[0]["winner"] == "draw"


def test_get_h2h_empty_response():
    from src.apis.sofascore import get_h2h
    with patch("src.apis.sofascore.get_json", return_value=None):
        assert get_h2h("1") == []


# ── get_team_form ──────────────────────────────────────────────────────────────

def test_get_team_form_returns_form_strings():
    from src.apis.sofascore import get_team_form
    payload = {
        "homeTeam": [{"value": "W"}, {"value": "W"}, {"value": "L"}],
        "awayTeam": [{"value": "D"}, {"value": "W"}],
    }
    with patch("src.apis.sofascore.get_json", return_value=payload):
        result = get_team_form("99")
    assert result["home"] == "WWL"
    assert result["away"] == "DW"
    assert result["source"] == "sofascore"


def test_get_team_form_empty():
    from src.apis.sofascore import get_team_form
    with patch("src.apis.sofascore.get_json", return_value=None):
        assert get_team_form("1") == {}


# ── get_event_statistics ───────────────────────────────────────────────────────

def test_get_event_statistics_flattens_groups():
    from src.apis.sofascore import get_event_statistics
    payload = {"statistics": [{
        "period": "ALL",
        "groups": [{"statisticsItems": [
            {"name": "Ball Possession", "home": "55%", "away": "45%"},
            {"name": "Total Shots",     "home": "12",  "away": "8"},
        ]}],
    }]}
    with patch("src.apis.sofascore.get_json", return_value=payload):
        result = get_event_statistics("42")
    assert result["ball_possession"] == {"home": "55%", "away": "45%"}
    assert result["total_shots"] == {"home": "12", "away": "8"}
    assert result["source"] == "sofascore"


# ── enrich_game_context ────────────────────────────────────────────────────────

def test_enrich_game_context_match_found():
    from src.apis.sofascore import enrich_game_context
    ev = _event(home="Los Angeles Lakers", away="Boston Celtics")

    with patch("src.apis.sofascore.get_scheduled_events", return_value=[{
        "id": "12345",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "home_score": 110, "away_score": 105,
        "status": "Finished", "status_type": "finished",
        "commence_time": "2024-11-15T00:00:00",
        "tournament": "NBA", "season": "2024/2025",
        "source": "sofascore",
    }]), patch("src.apis.sofascore.get_h2h", return_value=[{"winner": "home"}]):
        with patch("src.apis.sofascore.get_team_form", return_value={"home": "WWW", "away": "WLL"}):
            result = enrich_game_context("basketball_nba", "Lakers", "Celtics", "2024-11-15T19:00:00")

    assert result["available"] is True
    assert result["source"] == "sofascore"
    assert result["form"]["home"] == "WWW"


def test_enrich_game_context_no_match():
    from src.apis.sofascore import enrich_game_context
    with patch("src.apis.sofascore.get_scheduled_events", return_value=[]):
        result = enrich_game_context("basketball_nba", "MadeUpTeam", "AnotherFakeTeam", "2024-11-15T19:00:00")
    assert result["available"] is False


# ── get_standings ──────────────────────────────────────────────────────────────

def test_get_standings_parses_rows():
    from src.apis.sofascore import get_standings
    payload = {"standings": [{"rows": [
        {"position": 1, "team": {"id": 1, "name": "Arsenal"},
         "matches": 10, "wins": 8, "draws": 1, "losses": 1,
         "scoresFor": 20, "scoresAgainst": 8, "points": 25},
    ]}]}
    with patch("src.apis.sofascore.get_json", return_value=payload):
        result = get_standings("17", "52186")
    assert len(result) == 1
    assert result[0]["team"] == "Arsenal"
    assert result[0]["points"] == 25
    assert result[0]["source"] == "sofascore"
