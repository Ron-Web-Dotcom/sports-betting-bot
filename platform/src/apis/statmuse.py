"""
StatMuse data adapter.

StatMuse (statmuse.com) provides natural-language stat lookups for:
  - Player season/career/game-log stats
  - Team records and trends
  - Head-to-head matchup history
  - Prop-relevant lines (last N games averages)

No public API — we query their search endpoint and parse structured
JSON embedded in the HTML response (they use Next.js __NEXT_DATA__).
"""
import json
import logging
import re
from src.apis.base import get_html

logger = logging.getLogger(__name__)

_BASE = "https://www.statmuse.com"

# StatMuse sport path prefixes
_SPORT_PREFIX = {
    "basketball_nba":       "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb":         "mlb",
    "icehockey_nhl":        "nhl",
    "soccer_epl":           "fc",
    "soccer_usa_mls":       "fc",
}


def _extract_next_data(html: str) -> dict:
    """Pull structured data from Next.js __NEXT_DATA__ script tag."""
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _parse_stat_table(next_data: dict) -> list[dict]:
    """Extract rows from a StatMuse stat table response."""
    try:
        rows = (
            next_data
            .get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("players", [])
        )
        return rows
    except (AttributeError, KeyError):
        return []


def _parse_visual_summary(next_data: dict) -> str:
    """Extract the plain-English summary StatMuse generates for a query."""
    try:
        return (
            next_data
            .get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("summary", {})
            .get("text", "")
        )
    except (AttributeError, KeyError):
        return ""


# ── Player queries ─────────────────────────────────────────────────────────────

def player_season_stats(player_name: str, sport_key: str, season: str = "") -> dict:
    """
    Fetch a player's season averages from StatMuse.
    Example: player_season_stats("LeBron James", "basketball_nba", "2023-24")
    """
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    slug   = player_name.lower().replace(" ", "-")
    season_suffix = f"/{season}" if season else ""
    url    = f"{_BASE}/{prefix}/player/{slug}/stats{season_suffix}"

    html = get_html(url)
    if not html:
        return {"player": player_name, "sport": sport_key, "stats": {}, "source": "statmuse"}

    next_data = _extract_next_data(html)
    rows      = _parse_stat_table(next_data)
    summary   = _parse_visual_summary(next_data)

    stats = {}
    if rows:
        row = rows[0]
        for k, v in row.items():
            if isinstance(v, (int, float, str)):
                stats[k] = v

    return {
        "player":  player_name,
        "sport":   sport_key,
        "summary": summary,
        "stats":   stats,
        "source":  "statmuse",
    }


def player_last_n_games(player_name: str, sport_key: str, n: int = 5) -> dict:
    """
    Fetch a player's stats over their last N games.
    Critical for prop betting — recent form matters more than season average.
    """
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    slug   = player_name.lower().replace(" ", "-")
    url    = f"{_BASE}/{prefix}/player/{slug}/game-log/last-{n}-games"

    html = get_html(url)
    if not html:
        return {"player": player_name, "games": [], "averages": {}, "source": "statmuse"}

    next_data = _extract_next_data(html)
    rows      = _parse_stat_table(next_data)
    summary   = _parse_visual_summary(next_data)

    return {
        "player":   player_name,
        "sport":    sport_key,
        "n_games":  n,
        "summary":  summary,
        "games":    rows[:n],
        "source":   "statmuse",
    }


def player_vs_team(player_name: str, opponent: str, sport_key: str) -> dict:
    """Player's historical stats against a specific opponent."""
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    player_slug = player_name.lower().replace(" ", "-")
    team_slug   = opponent.lower().replace(" ", "-")
    url = f"{_BASE}/{prefix}/player/{player_slug}/stats/vs/{team_slug}"

    html = get_html(url)
    if not html:
        return {"player": player_name, "vs": opponent, "stats": {}, "source": "statmuse"}

    next_data = _extract_next_data(html)
    summary   = _parse_visual_summary(next_data)
    rows      = _parse_stat_table(next_data)

    return {
        "player":  player_name,
        "vs":      opponent,
        "sport":   sport_key,
        "summary": summary,
        "stats":   rows[0] if rows else {},
        "source":  "statmuse",
    }


# ── Team queries ───────────────────────────────────────────────────────────────

def team_season_stats(team_name: str, sport_key: str, season: str = "") -> dict:
    """Team's season stats including offensive/defensive ratings."""
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    slug   = team_name.lower().replace(" ", "-")
    season_suffix = f"/{season}" if season else ""
    url    = f"{_BASE}/{prefix}/team/{slug}/stats{season_suffix}"

    html = get_html(url)
    if not html:
        return {"team": team_name, "sport": sport_key, "stats": {}, "source": "statmuse"}

    next_data = _extract_next_data(html)
    summary   = _parse_visual_summary(next_data)
    rows      = _parse_stat_table(next_data)

    return {
        "team":    team_name,
        "sport":   sport_key,
        "summary": summary,
        "stats":   rows[0] if rows else {},
        "source":  "statmuse",
    }


def team_last_n_games(team_name: str, sport_key: str, n: int = 5) -> dict:
    """Team's recent form — W/L, scores, trends."""
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    slug   = team_name.lower().replace(" ", "-")
    url    = f"{_BASE}/{prefix}/team/{slug}/game-log/last-{n}-games"

    html = get_html(url)
    if not html:
        return {"team": team_name, "games": [], "source": "statmuse"}

    next_data = _extract_next_data(html)
    summary   = _parse_visual_summary(next_data)
    rows      = _parse_stat_table(next_data)

    return {
        "team":    team_name,
        "sport":   sport_key,
        "n_games": n,
        "summary": summary,
        "games":   rows[:n],
        "source":  "statmuse",
    }


def head_to_head(home_team: str, away_team: str, sport_key: str) -> dict:
    """Historical head-to-head record between two teams."""
    prefix     = _SPORT_PREFIX.get(sport_key, "nba")
    home_slug  = home_team.lower().replace(" ", "-")
    away_slug  = away_team.lower().replace(" ", "-")
    url        = f"{_BASE}/{prefix}/team/{home_slug}/history/vs/{away_slug}"

    html = get_html(url)
    if not html:
        return {"home": home_team, "away": away_team, "h2h": {}, "source": "statmuse"}

    next_data = _extract_next_data(html)
    summary   = _parse_visual_summary(next_data)
    rows      = _parse_stat_table(next_data)

    return {
        "home":    home_team,
        "away":    away_team,
        "sport":   sport_key,
        "summary": summary,
        "games":   rows[:10],
        "source":  "statmuse",
    }


# ── Natural-language query (versatile fallback) ────────────────────────────────

def query(question: str, sport_key: str = "basketball_nba") -> dict:
    """
    Send an arbitrary natural-language question to StatMuse.
    Examples:
      "LeBron James points per game last 10 games"
      "Chiefs record against the spread 2024"
      "Yankees ERA at home this season"
    """
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    params = {"q": question}
    url    = f"{_BASE}/{prefix}/ask"

    html = get_html(url, params=params)
    if not html:
        return {"query": question, "summary": "", "data": [], "source": "statmuse"}

    next_data = _extract_next_data(html)
    summary   = _parse_visual_summary(next_data)
    rows      = _parse_stat_table(next_data)

    return {
        "query":   question,
        "summary": summary,
        "data":    rows[:5],
        "source":  "statmuse",
    }
