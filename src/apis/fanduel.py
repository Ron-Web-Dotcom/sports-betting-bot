"""FanDuel sportsbook public API — live odds."""
import logging
import httpx

logger = logging.getLogger(__name__)

FD_BASE = "https://sbapi.fanduel.com/api"

SPORT_TAB_MAP = {
    "americanfootball_nfl":  "american-football/nfl",
    "basketball_nba":        "basketball/nba",
    "baseball_mlb":          "baseball/mlb",
    "icehockey_nhl":         "ice-hockey/nhl",
    "soccer_epl":            "soccer/english-premier-league",
    "americanfootball_ncaaf":"american-football/ncaaf",
}


def _get(path: str, params: dict | None = None) -> dict | None:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        resp = httpx.get(f"{FD_BASE}/{path}", params=params or {}, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("FanDuel error %s: %s", path, exc)
        return None


def get_events(sport_key: str) -> list[dict]:
    tab = SPORT_TAB_MAP.get(sport_key)
    if not tab:
        return []

    data = _get(f"content-managed-page", params={"page": "SPORT", "sport": tab})
    if not data:
        return []

    events = []
    for section in data.get("attachments", {}).get("events", {}).values():
        participants = section.get("teamParticipants") or section.get("players") or []
        team_names = [p.get("name", "") for p in participants]
        events.append(
            {
                "event_id":   str(section.get("eventId", "")),
                "event_name": section.get("name", " vs ".join(team_names)),
                "start_time": section.get("openDate", ""),
                "sport":      sport_key,
                "platform":   "fanduel",
                "markets":    [],  # enriched separately
            }
        )
    return events


def get_market_odds(event_id: str) -> list[dict]:
    data = _get(f"event/{event_id}/markets")
    if not data:
        return []

    markets = []
    for market in data.get("markets", []):
        runners = []
        for runner in market.get("runners", []):
            price = runner.get("winRunnerOdds", {}).get("americanDisplayOdds", {})
            runners.append(
                {
                    "selection": runner.get("runnerName", ""),
                    "odds": price.get("americanOdds"),
                }
            )
        markets.append(
            {
                "market_name": market.get("marketName", ""),
                "runners": runners,
            }
        )
    return markets


def fetch_all() -> list[dict]:
    all_events = []
    for sport_key in SPORT_TAB_MAP:
        events = get_events(sport_key)
        all_events.extend(events)
        logger.info("FanDuel: %d events for %s", len(events), sport_key)
    return all_events
