"""
Engine 2 — Sportsbook Comparison Engine.

Finds the best available odds across all books for every market.
Identifies and flags best-line opportunities.
"""
from dataclasses import dataclass
from src.engines.ev_engine import american_to_decimal, implied_prob, decimal_to_american


@dataclass
class BookComparison:
    market:    str
    selection: str
    books:     list[dict]   # [{book, american_odds, decimal_odds, implied_prob}]

    @property
    def best(self) -> dict:
        return max(self.books, key=lambda x: x["decimal_odds"])

    @property
    def worst(self) -> dict:
        return min(self.books, key=lambda x: x["decimal_odds"])

    @property
    def best_book(self) -> str:
        return self.best["book"]

    @property
    def worst_book(self) -> str:
        return self.worst["book"]

    @property
    def best_odds(self) -> int:
        return self.best["american_odds"]

    @property
    def worst_odds(self) -> int:
        return self.worst["american_odds"]

    @property
    def odds_range(self) -> int:
        """Absolute american-odds spread between best and worst book."""
        return abs(self.best["american_odds"] - self.worst["american_odds"])

    def discord_table(self) -> str:
        """Format a comparison table for Discord embed."""
        lines = []
        for entry in sorted(self.books, key=lambda x: x["decimal_odds"], reverse=True):
            prefix = "**" if entry["book"] == self.best_book else ""
            suffix = "** ✓" if entry["book"] == self.best_book else ""
            am = entry["american_odds"]
            sign = "+" if am > 0 else ""
            lines.append(f"{prefix}{entry['book'].title():18s} {sign}{am}{suffix}")
        lines.append(f"\n**Best Available:** {self.best_book.title()} {'+' if self.best_odds>0 else ''}{self.best_odds}")
        return "\n".join(lines)


def compare_market(event_norm: dict, market: str, selection: str) -> BookComparison | None:
    """Build a BookComparison for a selection across all available books."""
    entries = event_norm["markets"].get(market, {}).get(selection, [])
    if not entries:
        return None
    return BookComparison(market=market, selection=selection, books=entries)


def compare_all_markets(event_norm: dict) -> dict[str, dict[str, BookComparison]]:
    """
    Returns {market: {selection: BookComparison}} for every market in the event.
    """
    result: dict[str, dict[str, BookComparison]] = {}
    for market, selections in event_norm["markets"].items():
        result[market] = {}
        for sel, entries in selections.items():
            result[market][sel] = BookComparison(market=market, selection=sel, books=entries)
    return result


def find_line_discrepancies(
    comparisons: dict[str, dict[str, BookComparison]],
    min_delta_pct: float = 0.03,
) -> list[dict]:
    """
    Find cases where the best odds imply a probability more than min_delta_pct
    below the worst odds — indicating market inefficiency.
    """
    opportunities = []
    for market, sels in comparisons.items():
        for sel, comp in sels.items():
            if len(comp.books) < 2:
                continue
            delta = comp.best["implied_prob"] - comp.worst["implied_prob"]
            # Note: lower implied = better odds. Large gap = inefficiency.
            if abs(delta) >= min_delta_pct:
                opportunities.append({
                    "market":    market,
                    "selection": sel,
                    "best_book": comp.best_book,
                    "best_odds": comp.best_odds,
                    "worst_book":comp.worst_book,
                    "worst_odds":comp.worst["american_odds"],
                    "delta_pct": round(abs(delta), 4),
                })
    return sorted(opportunities, key=lambda x: x["delta_pct"], reverse=True)
