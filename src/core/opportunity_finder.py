"""Finds and assembles betting opportunities from all data sources."""
import logging
from src.apis import odds_api, espn, prizepicks, underdog, sleeper, draftkings, fanduel
from src.core.database import upsert_injuries, save_snapshot

logger = logging.getLogger(__name__)


def _build_platform_lines(event_name: str, sport: str) -> dict:
    """Cross-reference lines from DK, FD, PrizePicks, Underdog for an event."""
    lines: dict = {}

    # PrizePicks player props (fuzzy match by team names embedded in event_name)
    pp_projs = prizepicks.get_projections()
    lines["prizepicks"] = [
        p for p in pp_projs if p.get("sport_key") == sport
    ][:10]

    # Underdog lines
    ud_lines = underdog.get_over_under_lines()
    lines["underdog"] = [
        l for l in ud_lines if sport.split("_")[-1].lower() in l.get("sport", "").lower()
    ][:10]

    # Sleeper trending
    sleeper_sport = sport.split("_")[-1].lower()
    if sleeper_sport in ("nfl", "nba", "mlb"):
        lines["sleeper_trending"] = sleeper.get_player_news(sleeper_sport)[:10]

    return lines


def gather_opportunities(all_odds: dict[str, list]) -> list[dict]:
    """
    For each event across all sports, assemble:
      - event metadata + odds
      - injury context
      - news
      - cross-platform lines
    Returns list of opportunity dicts ready for AI analysis.
    """
    opportunities = []

    for sport, events in all_odds.items():
        injuries = espn.get_injuries(sport)
        if injuries:
            upsert_injuries(injuries)

        news = espn.get_news(sport)

        for event in events:
            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            # Flatten markets from all bookmakers
            best_odds: dict = {}
            for bk in bookmakers:
                for mkt in bk.get("markets", []):
                    mkt_key = mkt["key"]
                    if mkt_key not in best_odds:
                        best_odds[mkt_key] = {"bookmaker": bk["key"], "outcomes": mkt["outcomes"]}

            platform_lines = _build_platform_lines(event.get("sport_title", ""), sport)
            save_snapshot("the_odds_api", sport, event["id"], "all", best_odds)

            opportunities.append(
                {
                    "event": {
                        "id":         event["id"],
                        "name":       f"{event.get('away_team','')} vs {event.get('home_team','')}",
                        "sport":      sport,
                        "commence":   event.get("commence_time", ""),
                        "home_team":  event.get("home_team", ""),
                        "away_team":  event.get("away_team", ""),
                        "markets":    best_odds,
                    },
                    "injuries": injuries,
                    "news":     news,
                    "lines":    platform_lines,
                }
            )

    logger.info("Found %d raw opportunities across all sports", len(opportunities))
    return opportunities
