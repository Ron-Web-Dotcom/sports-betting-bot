"""DraftKings public sportsbook API — live odds / lines."""
import logging
import httpx

logger = logging.getLogger(__name__)

# DraftKings exposes a public offerings endpoint — no auth required
DK_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5"

SPORT_CATEGORY_MAP = {
    "americanfootball_nfl":  (88670846, "NFL"),
    "basketball_nba":        (42648,    "NBA"),
    "baseball_mlb":          (84240,    "MLB"),
    "icehockey_nhl":         (42133,    "NHL"),
    "soccer_epl":            (88808,    "Soccer"),
    "americanfootball_ncaaf":(87637,    "College Football"),
}


def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        resp = httpx.get(f"{DK_BASE}{path}", params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("DraftKings error %s: %s", path, exc)
        return None


def get_categories(sport_key: str) -> list[dict]:
    mapping = SPORT_CATEGORY_MAP.get(sport_key)
    if not mapping:
        return []
    category_id, _ = mapping
    data = _get(f"/eventgroups/{category_id}", {"format": "json"})
    if not data:
        return []

    events = []
    event_group = data.get("eventGroup", {})
    for event in event_group.get("events", []):
        odds_list = []
        for offer_category in event.get("offerCategories", []):
            for offer_subcategory in offer_category.get("offerSubcategoryDescriptors", []):
                for offer in offer_subcategory.get("offerSubcategory", {}).get("offers", []):
                    for outcome in offer:
                        odds_list.append(
                            {
                                "label": outcome.get("label"),
                                "odds":  outcome.get("oddsAmerican"),
                                "line":  outcome.get("line"),
                                "market": offer_subcategory.get("name"),
                            }
                        )
        events.append(
            {
                "event_id":   str(event.get("eventId", "")),
                "event_name": event.get("name", ""),
                "start_date": event.get("startDate", ""),
                "sport":      sport_key,
                "platform":   "draftkings",
                "odds":       odds_list,
            }
        )
    return events


def fetch_all() -> list[dict]:
    all_events = []
    for sport_key in SPORT_CATEGORY_MAP:
        events = get_categories(sport_key)
        all_events.extend(events)
        logger.info("DraftKings: %d events for %s", len(events), sport_key)
    return all_events
