"""PrizePicks public API — player props / projections."""
import logging
import httpx
from config.settings import PRIZEPICKS_API_BASE

logger = logging.getLogger(__name__)

SPORT_IDS = {
    "americanfootball_nfl":  "nfl",
    "basketball_nba":        "nba",
    "baseball_mlb":          "mlb",
    "icehockey_nhl":         "nhl",
    "basketball_ncaab":      "ncaab",
    "americanfootball_ncaaf":"ncaaf",
}


def _get(path: str, params: dict | None = None) -> dict | None:
    headers = {"Content-Type": "application/json"}
    try:
        resp = httpx.get(
            f"{PRIZEPICKS_API_BASE}{path}",
            params=params or {},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("PrizePicks error %s: %s", path, exc)
        return None


def get_projections(league: str | None = None) -> list[dict]:
    params = {"single_stat": "true"}
    if league:
        params["league_id"] = league
    data = _get("/projections", params)
    if not data:
        return []

    projections = []
    included = {item["id"]: item for item in data.get("included", [])}

    for proj in data.get("data", []):
        attrs = proj.get("attributes", {})
        rels = proj.get("relationships", {})

        player_id = rels.get("new_player", {}).get("data", {}).get("id")
        player_data = included.get(player_id, {})
        player_attrs = player_data.get("attributes", {})

        projections.append(
            {
                "id": proj.get("id"),
                "stat_type": attrs.get("stat_type"),
                "line_score": attrs.get("line_score"),
                "start_time": attrs.get("start_time"),
                "player_name": player_attrs.get("display_name", ""),
                "team": player_attrs.get("team", ""),
                "position": player_attrs.get("position", ""),
                "league": attrs.get("league", ""),
            }
        )
    return projections


def get_all_projections() -> list[dict]:
    all_projs = []
    for sport_key, league in SPORT_IDS.items():
        projs = get_projections(league)
        for p in projs:
            p["sport_key"] = sport_key
        all_projs.extend(projs)
        logger.info("PrizePicks: %d projections for %s", len(projs), league)
    return all_projs
