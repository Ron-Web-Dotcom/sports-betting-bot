"""Arbitrage detector — finds guaranteed-profit opportunities across books."""
import logging
from itertools import combinations
from dataclasses import dataclass
from src.apis.odds_api import american_to_decimal, implied_prob
from src.core.database import insert_arb
from config.settings import ARB_MIN_PROFIT_PCT

logger = logging.getLogger(__name__)


@dataclass
class ArbOpportunity:
    event_id:   str
    event_name: str
    sport:      str
    market:     str
    # Side A
    book_a:     str
    selection_a: str
    american_odds_a: int
    # Side B
    book_b:     str
    selection_b: str
    american_odds_b: int
    # Financials
    guaranteed_profit_pct: float
    stake_a: float = 0.0
    stake_b: float = 0.0

    def __str__(self) -> str:
        return (
            f"ARB {self.guaranteed_profit_pct:.2%} | {self.event_name} | {self.market} | "
            f"{self.book_a}({self.selection_a} {self.american_odds_a:+d}) vs "
            f"{self.book_b}({self.selection_b} {self.american_odds_b:+d})"
        )


def _arb_profit(dec_a: float, dec_b: float) -> float:
    """
    Two-outcome arb profit % for a given total stake of 1 unit.
    Positive = guaranteed profit.
    """
    sum_inv = (1 / dec_a) + (1 / dec_b)
    if sum_inv >= 1.0:
        return 0.0  # no arb
    return (1 / sum_inv) - 1


def _optimal_stakes(dec_a: float, dec_b: float, total: float) -> tuple[float, float]:
    """Split total stake optimally to guarantee equal profit on both outcomes."""
    sum_inv = (1 / dec_a) + (1 / dec_b)
    stake_a = total * (1 / dec_a) / sum_inv
    stake_b = total * (1 / dec_b) / sum_inv
    return round(stake_a, 2), round(stake_b, 2)


def scan_event_for_arbs(event: dict) -> list[ArbOpportunity]:
    """
    Scan all bookmakers in an Odds API event for two-outcome arb opportunities.
    Covers h2h and spreads markets.
    """
    arbs: list[ArbOpportunity] = []
    sport     = event.get("sport_key", "")
    event_id  = event.get("id", "")
    event_name = f"{event.get('away_team','')} vs {event.get('home_team','')}"

    # Build map: market_key -> selection -> {book: american_odds}
    book_market: dict[str, dict[str, dict[str, int]]] = {}
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            mk = mkt["key"]
            book_market.setdefault(mk, {})
            for outcome in mkt.get("outcomes", []):
                sel = outcome["name"]
                book_market[mk].setdefault(sel, {})
                book_market[mk][sel][bk["key"]] = int(outcome.get("price", -110))

    for mkt_key, selections in book_market.items():
        sel_names = list(selections.keys())
        # Two-outcome markets: h2h has 2-3 outcomes (include draw), spreads has 2
        if len(sel_names) < 2:
            continue

        # For each pair of selections (opposing sides)
        for sel_a, sel_b in combinations(sel_names, 2):
            # Find best odds for each selection across all books
            best_a = max(selections[sel_a].items(), key=lambda x: american_to_decimal(x[1]))
            best_b = max(selections[sel_b].items(), key=lambda x: american_to_decimal(x[1]))

            book_a, odds_a = best_a
            book_b, odds_b = best_b

            if book_a == book_b:
                continue  # same book, not an arb

            dec_a = american_to_decimal(odds_a)
            dec_b = american_to_decimal(odds_b)
            profit_pct = _arb_profit(dec_a, dec_b)

            if profit_pct >= ARB_MIN_PROFIT_PCT:
                stake_a, stake_b = _optimal_stakes(dec_a, dec_b, 100.0)
                arb = ArbOpportunity(
                    event_id=event_id,
                    event_name=event_name,
                    sport=sport,
                    market=mkt_key,
                    book_a=book_a,
                    selection_a=sel_a,
                    american_odds_a=odds_a,
                    book_b=book_b,
                    selection_b=sel_b,
                    american_odds_b=odds_b,
                    guaranteed_profit_pct=profit_pct,
                    stake_a=stake_a,
                    stake_b=stake_b,
                )
                arbs.append(arb)
                logger.info("ARB found: %s", arb)

    return arbs


def scan_all_events(all_odds: dict[str, list[dict]]) -> list[ArbOpportunity]:
    """Scan all sports/events for arbs. Saves each to DB."""
    all_arbs = []
    for sport, events in all_odds.items():
        for event in events:
            arbs = scan_event_for_arbs(event)
            for arb in arbs:
                insert_arb({
                    "sport":   arb.sport,
                    "event_id":  arb.event_id,
                    "event_name":arb.event_name,
                    "market":    arb.market,
                    "book_a":    arb.book_a,
                    "book_b":    arb.book_b,
                    "odds_a":    arb.american_odds_a,
                    "odds_b":    arb.american_odds_b,
                    "selection_a": arb.selection_a,
                    "selection_b": arb.selection_b,
                    "guaranteed_profit_pct": arb.guaranteed_profit_pct,
                    "stake_a":   arb.stake_a,
                    "stake_b":   arb.stake_b,
                })
            all_arbs.extend(arbs)
    logger.info("Arb scan complete — %d opportunities found", len(all_arbs))
    return all_arbs
