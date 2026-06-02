"""ESPN public API — stats, injuries, schedules."""
import logging
import httpx
from config.settings import ESPN_API_BASE, ESPN_SPORT_MAP

logger = logging.getLogger(__name__)


def _get(url: str, params: dict | None = None) -> dict | None:
    try:
        resp = httpx.get(url, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("ESPN error %s: %s", url, exc)
        return None


def get_injuries(odds_sport_key: str) -> list[dict]:
    mapping = ESPN_SPORT_MAP.get(odds_sport_key)
    if not mapping:
        return []
    sport, league = mapping
    url = f"{ESPN_API_BASE}/{sport}/{league}/injuries"
    data = _get(url)
    if not data:
        return []

    injuries = []
    for item in data.get("injuries", []):
        athlete = item.get("athlete", {})
        injuries.append(
            {
                "sport": odds_sport_key,
                "player_name": athlete.get("displayName", ""),
                "team": item.get("team", {}).get("displayName", ""),
                "status": item.get("status", ""),
                "detail": item.get("longComment", item.get("shortComment", "")),
            }
        )
    return injuries


def get_scoreboard(odds_sport_key: str) -> list[dict]:
    mapping = ESPN_SPORT_MAP.get(odds_sport_key)
    if not mapping:
        return []
    sport, league = mapping
    url = f"{ESPN_API_BASE}/{sport}/{league}/scoreboard"
    data = _get(url)
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])
        teams = [c.get("team", {}).get("displayName", "") for c in competitors]
        scores = [c.get("score", "0") for c in competitors]
        games.append(
            {
                "id": event.get("id"),
                "name": event.get("name"),
                "status": event.get("status", {}).get("type", {}).get("description", ""),
                "teams": teams,
                "scores": scores,
                "date": event.get("date"),
            }
        )
    return games


def get_team_stats(odds_sport_key: str, team_id: str) -> dict | None:
    mapping = ESPN_SPORT_MAP.get(odds_sport_key)
    if not mapping:
        return None
    sport, league = mapping
    url = f"{ESPN_API_BASE}/{sport}/{league}/teams/{team_id}/statistics"
    return _get(url)


def get_news(odds_sport_key: str) -> list[dict]:
    mapping = ESPN_SPORT_MAP.get(odds_sport_key)
    if not mapping:
        return []
    sport, league = mapping
    url = f"{ESPN_API_BASE}/{sport}/{league}/news"
    data = _get(url)
    if not data:
        return []
    articles = []
    for item in data.get("articles", [])[:10]:
        articles.append(
            {
                "headline": item.get("headline", ""),
                "description": item.get("description", ""),
                "published": item.get("published", ""),
            }
        )
    return articles


def fetch_all_injuries() -> dict[str, list[dict]]:
    result = {}
    for sport_key in ESPN_SPORT_MAP:
        inj = get_injuries(sport_key)
        if inj:
            result[sport_key] = inj
            logger.info("ESPN injuries: %d for %s", len(inj), sport_key)
    return result
