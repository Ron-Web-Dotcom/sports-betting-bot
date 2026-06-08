"""
StatMuse data adapter.

StatMuse (statmuse.com) provides natural-language stat lookups for:
  - Player season/career/game-log stats
  - Team records and trends
  - Head-to-head matchup history
  - Prop-relevant lines (last N games averages)

No public API — we use their /ask natural-language endpoint and parse
structured JSON embedded in the HTML response (Next.js __NEXT_DATA__).

IMPORTANT: Direct player URLs require a numeric ID suffix (e.g. /nba/player/lebron-james-1234)
which we don't have at query time. All lookups go through /ask instead.

Confirmed sport prefixes (statmuse.com):
  nba   — NBA basketball
  nfl   — NFL football
  mlb   — MLB baseball
  nhl   — NHL hockey
  fc    — ALL soccer (EPL, MLS, La Liga, Bundesliga, World Cup, etc.)
  wnba  — WNBA
  cfb   — College football (NCAAF)
  pga   — PGA Tour golf

NOT covered by StatMuse: MMA/UFC, Tennis, NASCAR, Boxing, NCAAB, Esports
"""
import json
import logging
import re
from src.apis.base import get_html

logger = logging.getLogger(__name__)

_BASE = "https://www.statmuse.com"

# Confirmed sport prefixes — research verified June 2026
_SPORT_PREFIX = {
    "basketball_nba":                 "nba",
    "basketball_ncaab":               "nba",    # NCAAB uses nba ask endpoint
    "americanfootball_nfl":           "nfl",
    "americanfootball_ncaaf":         "cfb",    # College football has own prefix
    "baseball_mlb":                   "mlb",
    "icehockey_nhl":                  "nhl",
    "soccer_epl":                     "fc",
    "soccer_usa_mls":                 "fc",
    "soccer_fifa_world_cup":          "fc",
    "golf_masters_tournament_winner": "pga",
    "mma_mixed_martial_arts":         None,     # Not covered
    "tennis_atp_french_open":         None,     # Not covered
    "boxing_boxing":                  None,     # Not covered
    "motorsport_formula_1":           None,     # Not covered
    "esports_lol":                    None,     # Not covered
}

# Human-readable sport label for query construction
_SPORT_LABEL = {
    "basketball_nba":                 "NBA",
    "basketball_ncaab":               "college basketball",
    "americanfootball_nfl":           "NFL",
    "americanfootball_ncaaf":         "college football",
    "baseball_mlb":                   "MLB",
    "icehockey_nhl":                  "NHL",
    "soccer_epl":                     "soccer",
    "soccer_usa_mls":                 "MLS soccer",
    "soccer_fifa_world_cup":          "World Cup soccer",
    "golf_masters_tournament_winner": "PGA golf",
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
        return (
            next_data
            .get("props", {})
            .get("pageProps", {})
            .get("data", {})
            .get("players", [])
        )
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


def _ask(question: str, prefix: str) -> dict:
    """
    Core query function — sends natural language question to StatMuse /ask.
    Returns {summary, rows, source}.
    """
    url = f"{_BASE}/{prefix}/ask"
    # StatMuse /ask uses ?q= with + as space separator
    params = {"q": question.replace(" ", "+")}
    html = get_html(url, params=params)
    if not html:
        logger.warning("StatMuse ask failed: %s/%s q=%s", prefix, "ask", question)
        return {"summary": "", "rows": [], "source": "statmuse"}

    next_data = _extract_next_data(html)
    return {
        "summary": _parse_visual_summary(next_data),
        "rows":    _parse_stat_table(next_data),
        "source":  "statmuse",
    }


# ── Public functions ───────────────────────────────────────────────────────────

def player_last_n_games(player_name: str, sport_key: str, n: int = 5) -> dict:
    """
    Fetch a player's stats over their last N games via natural-language query.
    Works for all StatMuse-covered sports across all seasons.
    """
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        logger.debug("StatMuse: sport %s not covered", sport_key)
        return {"player": player_name, "games": [], "summary": "", "source": "statmuse"}

    result = _ask(f"{player_name} last {n} games", prefix)
    return {
        "player":  player_name,
        "sport":   sport_key,
        "n_games": n,
        "summary": result["summary"],
        "games":   result["rows"][:n],
        "source":  "statmuse",
    }


def player_season_stats(player_name: str, sport_key: str, season: str = "") -> dict:
    """Fetch a player's season averages via natural-language query."""
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        return {"player": player_name, "stats": {}, "summary": "", "source": "statmuse"}

    season_str = f"{season} season" if season else "this season"
    result = _ask(f"{player_name} stats {season_str}", prefix)
    stats = {}
    if result["rows"]:
        row = result["rows"][0]
        stats = {k: v for k, v in row.items() if isinstance(v, (int, float, str))}

    return {
        "player":  player_name,
        "sport":   sport_key,
        "summary": result["summary"],
        "stats":   stats,
        "source":  "statmuse",
    }


def player_vs_team(player_name: str, opponent: str, sport_key: str) -> dict:
    """Player's historical stats against a specific opponent."""
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        return {"player": player_name, "vs": opponent, "stats": {}, "source": "statmuse"}

    result = _ask(f"{player_name} stats vs {opponent}", prefix)
    return {
        "player":  player_name,
        "vs":      opponent,
        "sport":   sport_key,
        "summary": result["summary"],
        "stats":   result["rows"][0] if result["rows"] else {},
        "source":  "statmuse",
    }


def team_season_stats(team_name: str, sport_key: str, season: str = "") -> dict:
    """Team's season stats."""
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        return {"team": team_name, "stats": {}, "source": "statmuse"}

    season_str = f"{season} season" if season else "this season"
    result = _ask(f"{team_name} stats {season_str}", prefix)
    return {
        "team":    team_name,
        "sport":   sport_key,
        "summary": result["summary"],
        "stats":   result["rows"][0] if result["rows"] else {},
        "source":  "statmuse",
    }


def team_last_n_games(team_name: str, sport_key: str, n: int = 5) -> dict:
    """Team's recent form."""
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        return {"team": team_name, "games": [], "source": "statmuse"}

    result = _ask(f"{team_name} last {n} games", prefix)
    return {
        "team":    team_name,
        "sport":   sport_key,
        "n_games": n,
        "summary": result["summary"],
        "games":   result["rows"][:n],
        "source":  "statmuse",
    }


def head_to_head(home_team: str, away_team: str, sport_key: str) -> dict:
    """Historical head-to-head record between two teams."""
    prefix = _SPORT_PREFIX.get(sport_key)
    if not prefix:
        return {"home": home_team, "away": away_team, "h2h": {}, "source": "statmuse"}

    result = _ask(f"{home_team} vs {away_team} history", prefix)
    return {
        "home":    home_team,
        "away":    away_team,
        "sport":   sport_key,
        "summary": result["summary"],
        "games":   result["rows"][:10],
        "source":  "statmuse",
    }


def query(question: str, sport_key: str = "basketball_nba") -> dict:
    """
    Send an arbitrary natural-language question to StatMuse.
    Examples:
      "LeBron James points per game last 10 games"
      "Chiefs record against the spread 2024"
      "Yankees ERA at home this season"
      "Messi goals in World Cup 2022"
    """
    prefix = _SPORT_PREFIX.get(sport_key, "nba")
    if not prefix:
        return {"query": question, "summary": "", "data": [], "source": "statmuse"}

    result = _ask(question, prefix)
    return {
        "query":   question,
        "summary": result["summary"],
        "data":    result["rows"][:5],
        "source":  "statmuse",
    }
