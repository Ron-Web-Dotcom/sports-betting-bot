"""Tests for line shopping across books."""
import pytest
from src.core.line_shopper import find_best_line, extract_book_odds, shop_all_markets


def _make_event() -> dict:
    return {
        "id": "evt1",
        "sport_key": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Lakers",  "price": -115},
                    {"name": "Celtics", "price": -105},
                ]}],
            },
            {
                "key": "fanduel",
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Lakers",  "price": -108},   # better for Lakers
                    {"name": "Celtics", "price": -112},
                ]}],
            },
        ],
    }


def test_extract_book_odds():
    event = _make_event()
    odds = extract_book_odds(event, "h2h", "Lakers")
    books = {o["book"] for o in odds}
    assert "draftkings" in books and "fanduel" in books


def test_find_best_line_picks_highest_odds():
    event = _make_event()
    book_odds = extract_book_odds(event, "h2h", "Lakers")
    best = find_best_line("evt1", "Lakers vs Celtics", "basketball_nba", "h2h", "Lakers", book_odds)
    assert best is not None
    # FanDuel offers -108 vs DK -115 — FD is better (less negative = higher decimal)
    assert best.book == "fanduel"
    assert best.american_odds == -108


def test_shop_all_markets():
    event = _make_event()
    markets = shop_all_markets(event)
    assert "h2h" in markets
    assert "Lakers" in markets["h2h"]
    assert "Celtics" in markets["h2h"]


def test_no_bookmakers_returns_none():
    result = find_best_line("e1", "A vs B", "nba", "h2h", "A", [])
    assert result is None
