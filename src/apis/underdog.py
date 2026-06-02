"""Underdog Fantasy public API — player pick'em lines."""
import logging
import httpx
from config.settings import UNDERDOG_API_BASE

logger = logging.getLogger(__name__)


def _get(path: str, params: dict | None = None) -> dict | None:
    headers = {"Accept": "application/json"}
    try:
        resp = httpx.get(
            f"{UNDERDOG_API_BASE}{path}",
            params=params or {},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.error("Underdog error %s: %s", path, exc)
        return None


def get_over_under_lines() -> list[dict]:
    data = _get("/beta/over_under_lines")
    if not data:
        return []

    lines = []
    appearances = {a["id"]: a for a in data.get("appearances", [])}
    players      = {p["id"]: p for p in data.get("players", [])}
    match_ups    = {m["id"]: m for m in data.get("match_ups", [])}

    for line in data.get("over_under_lines", []):
        app_id  = line.get("over_under", {}).get("appearance_stat", {}).get("appearance_id")
        app     = appearances.get(app_id, {})
        player  = players.get(app.get("player_id"), {})
        match   = match_ups.get(app.get("match_up_id"), {})

        lines.append(
            {
                "id": line.get("id"),
                "stat_value": line.get("stat_value"),
                "stat_type": line.get("over_under", {}).get("appearance_stat", {}).get("display_stat", ""),
                "player_name": f"{player.get('first_name','')} {player.get('last_name','')}".strip(),
                "team": app.get("team_name", ""),
                "position": player.get("position", ""),
                "sport": match.get("sport_id", ""),
                "game_date": match.get("scheduled_at", ""),
            }
        )
    return lines
