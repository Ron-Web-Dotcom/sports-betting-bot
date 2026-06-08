"""Comprehensive tests for comparison_engine — target 100% coverage."""
import pytest
from src.engines.comparison_engine import (
    BookComparison, compare_market, compare_all_markets, find_line_discrepancies
)


def _books(*odds_pairs):
    """Helper: build books list from (book_name, american_odds) tuples."""
    from src.engines.ev_engine import american_to_decimal, implied_prob
    return [
        {
            "book": name,
            "american_odds": odds,
            "decimal_odds": american_to_decimal(odds),
            "implied_prob": implied_prob(odds),
        }
        for name, odds in odds_pairs
    ]


def _event(market="h2h", selection="Team A", books_data=None):
    if books_data is None:
        books_data = [("draftkings", -110), ("fanduel", -108), ("betmgm", -115)]
    return {
        "markets": {
            market: {
                selection: _books(*books_data)
            }
        }
    }


# ── BookComparison properties ─────────────────────────────────────────────────

def test_best_returns_highest_decimal_odds():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105), ("mgm", -115)))
    assert comp.best["book"] == "fd"


def test_worst_returns_lowest_decimal_odds():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105), ("mgm", -115)))
    assert comp.worst["book"] == "mgm"


def test_best_book_property():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105)))
    assert comp.best_book == "fd"


def test_best_odds_property():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105)))
    assert comp.best_odds == -105


def test_odds_range():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", +100)))
    # abs(best_american - worst_american) — best is +100, worst is -110
    assert comp.odds_range == 210   # abs(100 - (-110)) = 210


def test_discord_table_contains_best_book():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105)))
    table = comp.discord_table()
    assert "fd" in table.lower() or "Fd" in table


def test_discord_table_marks_best_with_checkmark():
    comp = BookComparison("h2h", "TeamA", _books(("dk", -110), ("fd", -105)))
    table = comp.discord_table()
    assert "✓" in table


def test_discord_table_plus_sign_for_positive_odds():
    comp = BookComparison("h2h", "TeamA", _books(("dk", +150), ("fd", +120)))
    table = comp.discord_table()
    assert "+" in table


# ── compare_market ────────────────────────────────────────────────────────────

def test_compare_market_returns_book_comparison():
    event = _event()
    result = compare_market(event, "h2h", "Team A")
    assert isinstance(result, BookComparison)
    assert result.market == "h2h"
    assert result.selection == "Team A"


def test_compare_market_returns_none_for_missing_selection():
    event = _event()
    assert compare_market(event, "h2h", "Team Z") is None


def test_compare_market_returns_none_for_missing_market():
    event = _event()
    assert compare_market(event, "spreads", "Team A") is None


def test_compare_market_single_book():
    event = _event(books_data=[("dk", -110)])
    result = compare_market(event, "h2h", "Team A")
    assert result is not None
    assert len(result.books) == 1


# ── compare_all_markets ───────────────────────────────────────────────────────

def test_compare_all_markets_returns_nested_dict():
    event = _event()
    result = compare_all_markets(event)
    assert "h2h" in result
    assert "Team A" in result["h2h"]
    assert isinstance(result["h2h"]["Team A"], BookComparison)


def test_compare_all_markets_multiple_markets():
    event = {
        "markets": {
            "h2h":     {"TeamA": _books(("dk", -110), ("fd", -108))},
            "spreads": {"TeamA -1.5": _books(("dk", -110))},
        }
    }
    result = compare_all_markets(event)
    assert "h2h" in result
    assert "spreads" in result


def test_compare_all_markets_empty_event():
    event = {"markets": {}}
    result = compare_all_markets(event)
    assert result == {}


# ── find_line_discrepancies ───────────────────────────────────────────────────

def test_find_discrepancies_detects_large_spread():
    # -110 vs +150 is a huge discrepancy
    comps = compare_all_markets(_event(books_data=[("dk", -110), ("fd", +150)]))
    discrepancies = find_line_discrepancies(comps, min_delta_pct=0.01)
    assert len(discrepancies) > 0
    assert discrepancies[0]["delta_pct"] > 0.01


def test_find_discrepancies_ignores_small_spread():
    # -110 vs -109 is noise
    comps = compare_all_markets(_event(books_data=[("dk", -110), ("fd", -109)]))
    discrepancies = find_line_discrepancies(comps, min_delta_pct=0.10)
    assert len(discrepancies) == 0


def test_find_discrepancies_sorted_by_delta_desc():
    event = {
        "markets": {
            "h2h": {
                "TeamA": _books(("dk", -110), ("fd", +200)),  # large
                "TeamB": _books(("dk", -110), ("fd", -108)),  # small
            }
        }
    }
    comps = compare_all_markets(event)
    result = find_line_discrepancies(comps, min_delta_pct=0.001)
    if len(result) >= 2:
        assert result[0]["delta_pct"] >= result[1]["delta_pct"]


def test_find_discrepancies_single_book_skipped():
    comps = compare_all_markets(_event(books_data=[("dk", -110)]))
    result = find_line_discrepancies(comps, min_delta_pct=0.0)
    assert len(result) == 0  # need at least 2 books to detect discrepancy
