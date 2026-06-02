"""Line shopper — finds the best available odds for a selection across all books."""
import logging
from dataclasses import dataclass
from src.apis.odds_api import american_to_decimal

logger = logging.getLogger(__name__)


@dataclass
class BestLine:
    event_id:     str
    event_name:   str
    sport:        str
    market:       str
    selection:    str
    book:         str
    american_odds: int
    decimal_odds:  float
    all_books:    list[dict]  # [{book, american_odds}, ...]


def find_best_line(
    event_id: str,
    event_name: str,
    sport: str,
    market: str,
    selection: str,
    bookmaker_odds: list[dict],  # [{"book": str, "american_odds": int}]
) -> BestLine | None:
    """Return the book offering the best (highest) odds for the selection."""
    if not bookmaker_odds:
        return None

    best = max(bookmaker_odds, key=lambda x: american_to_decimal(x["american_odds"]))
    return BestLine(
        event_id=event_id,
        event_name=event_name,
        sport=sport,
        market=market,
        selection=selection,
        book=best["book"],
        american_odds=best["american_odds"],
        decimal_odds=american_to_decimal(best["american_odds"]),
        all_books=bookmaker_odds,
    )


def extract_book_odds(event: dict, market_key: str, selection_name: str) -> list[dict]:
    """
    From an Odds API event dict, extract all book odds for a given market + selection.
    Returns [{"book": str, "american_odds": int}, ...]
    """
    results = []
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt["key"] != market_key:
                continue
            for outcome in mkt.get("outcomes", []):
                if outcome["name"].lower() == selection_name.lower():
                    results.append({
                        "book": bk["key"],
                        "american_odds": int(outcome.get("price", 0)),
                    })
    return results


def shop_all_markets(event: dict) -> dict[str, dict[str, BestLine]]:
    """
    Returns {market_key: {selection_name: BestLine}} for every market in the event.
    """
    sport     = event.get("sport_key", "")
    event_id  = event.get("id", "")
    event_name = f"{event.get('away_team','')} vs {event.get('home_team','')}"

    # Collect all selections per market
    markets: dict[str, set] = {}
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            mk = mkt["key"]
            markets.setdefault(mk, set())
            for outcome in mkt.get("outcomes", []):
                markets[mk].add(outcome["name"])

    result: dict[str, dict[str, BestLine]] = {}
    for mkt_key, selections in markets.items():
        result[mkt_key] = {}
        for sel in selections:
            book_odds = extract_book_odds(event, mkt_key, sel)
            best = find_best_line(event_id, event_name, sport, mkt_key, sel, book_odds)
            if best:
                result[mkt_key][sel] = best

    return result
