"""
Stats Perform / Opta adapter.

Stats Perform (formerly Opta) is an enterprise sports data provider offering
deep advanced analytics: expected goals (xG), PPDA, progressive passes, shot
maps, player heat maps, match momentum, and live win probability models.

Strongest coverage: soccer/European football. Also covers NFL, NBA, MLB, NHL.

Enterprise API — requires a contracted API key.
  Set env var: STATSPERFORM_API_KEY

Base URL: https://api.statsperform.com/

NOTE: Exact endpoint paths vary by client contract with Stats Perform.
The patterns below follow their common REST conventions but may need
adjustment depending on which data packages your contract includes.

Standard endpoint patterns:
  GET /soccer/competition/{comp_id}/matches       match list for competition
  GET /soccer/match/{match_id}/statistics         detailed advanced stats
  GET /soccer/team/{team_id}/form                 recent form with metrics
  GET /soccer/player/{player_id}/statistics       player season stats
  GET /soccer/match/{match_id}/win-probability    live win probability
  (replace 'soccer' with sport slug for other sports)
"""
import logging
import os

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.statsperform.com"

# Maps our internal sport_key prefix → Stats Perform sport slug
SPORT_MAP = {
    "soccer":           "soccer",
    "americanfootball": "americanfootball",
    "basketball":       "basketball",
    "baseball":         "baseball",
    "icehockey":        "icehockey",
}

# Maps full sport_key → Stats Perform sport slug
_SPORT_KEY_MAP = {
    "americanfootball_nfl":      "americanfootball",
    "basketball_nba":            "basketball",
    "baseball_mlb":              "baseball",
    "icehockey_nhl":             "icehockey",
    "soccer_epl":                "soccer",
    "soccer_uefa_champs_league": "soccer",
    "soccer_usa_mls":            "soccer",
    "soccer_bundesliga":         "soccer",
    "soccer_la_liga":            "soccer",
    "soccer_serie_a":            "soccer",
    "soccer_ligue_1":            "soccer",
    "soccer_eredivisie":         "soccer",
    "soccer_portugal_primeira_liga": "soccer",
}


def _api_key() -> str | None:
    return os.environ.get("STATSPERFORM_API_KEY") or None


def _unavailable(reason: str = "STATSPERFORM_API_KEY not configured") -> dict:
    return {"available": False, "reason": reason, "source": "statsperform"}


def _sport_slug(sport_key: str) -> str | None:
    """Resolve a sport_key to a Stats Perform sport slug."""
    slug = _SPORT_KEY_MAP.get(sport_key)
    if slug:
        return slug
    # Fall back to prefix matching (e.g. "soccer_*" → "soccer")
    prefix = sport_key.split("_")[0]
    return SPORT_MAP.get(prefix)


def _get(path: str, params: dict | None = None) -> dict | list | None:
    """
    Authenticated GET to the Stats Perform API.
    Returns parsed JSON or None on any failure.
    """
    key = _api_key()
    if not key:
        return None
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    url = f"{_BASE}{path}"
    return get_json(url, params=params, headers=headers)


# ── Match statistics ──────────────────────────────────────────────────────────

def get_match_stats(sport_key: str, match_id: str) -> dict:
    """
    Return advanced match statistics for a given match.

    Includes (where available from your contract):
      xG (expected goals), shots on/off target, possession, progressive
      passes, PPDA (passes allowed per defensive action), pressing metrics,
      shot map data, and period-level breakdowns.

    sport_key: internal key e.g. "soccer_epl"
    match_id:  Stats Perform match identifier
    """
    slug = _sport_slug(sport_key)
    if not slug:
        logger.warning("statsperform: unsupported sport_key %r", sport_key)
        return {}

    key = _api_key()
    if not key:
        logger.warning("statsperform: STATSPERFORM_API_KEY not set — skipping get_match_stats")
        return {}

    data = _get(f"/{slug}/match/{match_id}/statistics")
    if not data:
        return {}

    payload = data if isinstance(data, dict) else {}

    # Normalise common advanced stat keys into a flat result dict.
    # Actual field names depend on the Stats Perform feed version —
    # adjust these key paths to match your contracted feed schema.
    stats = payload.get("matchStats", payload.get("statistics", payload))
    home = stats.get("home", stats.get("homeTeam", {}))
    away = stats.get("away", stats.get("awayTeam", {}))

    return {
        "match_id":   match_id,
        "sport":      sport_key,
        # xG
        "xg_home":    home.get("expectedGoals", home.get("xg")),
        "xg_away":    away.get("expectedGoals", away.get("xg")),
        # Shots
        "shots_home": home.get("totalShots", home.get("shots")),
        "shots_away": away.get("totalShots", away.get("shots")),
        "shots_on_target_home": home.get("shotsOnTarget"),
        "shots_on_target_away": away.get("shotsOnTarget"),
        # Possession
        "possession_home": home.get("possessionPercentage", home.get("possession")),
        "possession_away": away.get("possessionPercentage", away.get("possession")),
        # Progressive passes (soccer-specific)
        "progressive_passes_home": home.get("progressivePasses"),
        "progressive_passes_away": away.get("progressivePasses"),
        # PPDA — passes per defensive action (pressing intensity)
        "ppda_home": home.get("ppda"),
        "ppda_away": away.get("ppda"),
        # Pressures
        "pressures_home": home.get("pressures"),
        "pressures_away": away.get("pressures"),
        # Raw payload available for any additional keys
        "raw": payload,
        "source": "statsperform",
    }


# ── Team form ─────────────────────────────────────────────────────────────────

def get_team_form(sport_key: str, team_id: str, n: int = 5) -> list[dict]:
    """
    Return recent form for a team — last n matches with advanced metrics.

    Each entry includes result, score, xG for/against, and basic context.

    sport_key: internal key e.g. "soccer_epl"
    team_id:   Stats Perform team identifier
    n:         number of recent matches to return (default 5)
    """
    slug = _sport_slug(sport_key)
    if not slug:
        logger.warning("statsperform: unsupported sport_key %r", sport_key)
        return []

    key = _api_key()
    if not key:
        logger.warning("statsperform: STATSPERFORM_API_KEY not set — skipping get_team_form")
        return []

    data = _get(f"/{slug}/team/{team_id}/form", params={"limit": n})
    if not data:
        return []

    matches = data if isinstance(data, list) else data.get("matches", data.get("form", []))
    results = []
    for m in matches[:n]:
        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        score = m.get("score", {})
        adv = m.get("advancedStats", m.get("statistics", {}))
        results.append({
            "match_id":       str(m.get("id", m.get("matchId", ""))),
            "date":           m.get("date", m.get("kickOff", "")),
            "home_team":      home.get("name", ""),
            "away_team":      away.get("name", ""),
            "home_score":     score.get("home", score.get("homeScore")),
            "away_score":     score.get("away", score.get("awayScore")),
            "xg_home":        adv.get("xgHome", adv.get("expectedGoalsHome")),
            "xg_away":        adv.get("xgAway", adv.get("expectedGoalsAway")),
            "ppda_home":      adv.get("ppdaHome"),
            "ppda_away":      adv.get("ppdaAway"),
            "possession_home": adv.get("possessionHome"),
            "possession_away": adv.get("possessionAway"),
            "source":         "statsperform",
        })
    return results


# ── Player statistics ─────────────────────────────────────────────────────────

def get_player_stats(sport_key: str, player_id: str, season: str | None = None) -> dict:
    """
    Return season-level advanced statistics for a player.

    Includes (where available): goals, assists, xG, xA, progressive passes,
    pressures, duels won, heat map reference, minutes played.

    sport_key: internal key e.g. "soccer_epl"
    player_id: Stats Perform player identifier
    season:    season identifier (e.g. "2024" or "2024/25"); defaults to current
    """
    slug = _sport_slug(sport_key)
    if not slug:
        logger.warning("statsperform: unsupported sport_key %r", sport_key)
        return {}

    key = _api_key()
    if not key:
        logger.warning("statsperform: STATSPERFORM_API_KEY not set — skipping get_player_stats")
        return {}

    params = {}
    if season:
        params["season"] = season

    data = _get(f"/{slug}/player/{player_id}/statistics", params=params or None)
    if not data:
        return {}

    payload = data if isinstance(data, dict) else {}
    stats = payload.get("playerStats", payload.get("statistics", payload))

    return {
        "player_id":          player_id,
        "sport":              sport_key,
        "season":             season or stats.get("season", ""),
        "appearances":        stats.get("appearances", stats.get("matchesPlayed")),
        "minutes_played":     stats.get("minutesPlayed"),
        "goals":              stats.get("goals"),
        "assists":            stats.get("assists"),
        "xg":                 stats.get("expectedGoals", stats.get("xg")),
        "xa":                 stats.get("expectedAssists", stats.get("xA")),
        "shots":              stats.get("totalShots", stats.get("shots")),
        "shots_on_target":    stats.get("shotsOnTarget"),
        "progressive_passes": stats.get("progressivePasses"),
        "key_passes":         stats.get("keyPasses"),
        "pressures":          stats.get("pressures"),
        "duels_won":          stats.get("duelsWon"),
        "dribbles_completed": stats.get("dribblesCompleted"),
        "heat_map_ref":       stats.get("heatMapUrl", stats.get("heatMap")),
        "raw":                payload,
        "source":             "statsperform",
    }


# ── Live win probability ───────────────────────────────────────────────────────

def get_live_win_probability(match_id: str) -> dict:
    """
    Return the real-time win probability model output for a live match.

    Stats Perform's probability model updates throughout the match based on
    current scoreline, time remaining, xG, possession context, and momentum.

    match_id: Stats Perform match identifier (soccer assumed; for other sports
              call _get() directly with the appropriate sport slug path)

    Returns: dict with home_prob, away_prob, draw_prob (each 0.0–1.0) or empty.
    """
    key = _api_key()
    if not key:
        logger.warning("statsperform: STATSPERFORM_API_KEY not set — skipping get_live_win_probability")
        return {}

    data = _get(f"/soccer/match/{match_id}/win-probability")
    if not data:
        return {}

    payload = data if isinstance(data, dict) else {}
    probs = payload.get("winProbability", payload.get("probability", payload))

    home_prob = probs.get("home", probs.get("homeProbability"))
    away_prob = probs.get("away", probs.get("awayProbability"))
    draw_prob = probs.get("draw", probs.get("drawProbability"))

    return {
        "match_id":   match_id,
        "home_prob":  home_prob,
        "away_prob":  away_prob,
        "draw_prob":  draw_prob,
        "minute":     probs.get("minute"),
        "momentum":   payload.get("momentum"),
        "raw":        payload,
        "source":     "statsperform",
    }


# ── Convenience: enrich a game context dict ───────────────────────────────────

def enrich_game_context(
    sport_key: str,
    home_team: str,
    away_team: str,
    match_id: str | None = None,
) -> dict:
    """
    Best-effort enrichment of a game with Stats Perform advanced data.

    If STATSPERFORM_API_KEY is not set, returns immediately with
    {"available": False, ...} so callers can degrade gracefully.

    When match_id is supplied, fetches match stats and live win probability
    directly. Without a match_id, only team form data is returned (requires
    team IDs which may need prior resolution — pass None to skip).

    Returns a dict always containing the "available" key.
    """
    key = _api_key()
    if not key:
        return _unavailable()

    slug = _sport_slug(sport_key)
    if not slug:
        return _unavailable(f"unsupported sport_key: {sport_key!r}")

    context: dict = {
        "available":  True,
        "sport":      sport_key,
        "home_team":  home_team,
        "away_team":  away_team,
        "source":     "statsperform",
    }

    if match_id:
        match_stats = get_match_stats(sport_key, match_id)
        if match_stats:
            context["match_stats"] = match_stats

        # Live win probability is soccer-only in the standard feed
        if slug == "soccer":
            win_prob = get_live_win_probability(match_id)
            if win_prob:
                context["win_probability"] = win_prob

    return context
