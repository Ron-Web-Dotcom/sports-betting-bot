"""
RotoWire injury and lineup adapter.

RotoWire provides up-to-the-minute injury news, confirmed starting lineups,
and daily notes. This is critical for same-day prop bets.

We scrape their public injury/news pages — no API key required.
"""
import logging
import re
from src.apis.base import get_html

logger = logging.getLogger(__name__)

_BASE = "https://www.rotowire.com"

SPORT_PATHS = {
    "americanfootball_nfl": "/football/injury-report.php",
    "basketball_nba":       "/basketball/injury-report.php",
    "baseball_mlb":         "/baseball/injury-report.php",
    "icehockey_nhl":        "/hockey/injury-report.php",
}

LINEUP_PATHS = {
    "basketball_nba": "/basketball/nba-lineups.php",
    "baseball_mlb":   "/baseball/mlb-lineups.php",
    "americanfootball_nfl": "/football/nfl-lineups.php",
}


def _parse_injury_table(html: str, sport_key: str) -> list[dict]:
    """Extract injury rows from RotoWire's injury report table."""
    injuries = []
    # RotoWire uses a consistent pattern: player name, team, position, status, detail
    # Match table rows with injury data
    pattern = re.compile(
        r'<a[^>]+player[^>]+>([^<]+)</a>.*?'  # player name
        r'<span[^>]+team[^>]*>([^<]+)</span>.*?'  # team
        r'<span[^>]+pos[^>]*>([^<]+)</span>.*?'  # position
        r'<span[^>]+status[^>]*>([^<]+)</span>.*?'  # status
        r'<td[^>]+detail[^>]*>([^<]+)</td>',      # detail
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        injuries.append({
            "player":   m.group(1).strip(),
            "team":     m.group(2).strip(),
            "position": m.group(3).strip(),
            "status":   m.group(4).strip().lower(),
            "details":  m.group(5).strip(),
            "sport":    sport_key,
            "source":   "rotowire",
        })

    # Fallback: look for JSON-LD or embedded data
    if not injuries:
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>', html, re.DOTALL)
        if json_match:
            try:
                import json
                state = json.loads(json_match.group(1))
                for entry in state.get("injuries", []):
                    injuries.append({
                        "player":   entry.get("playerName", ""),
                        "team":     entry.get("team", ""),
                        "position": entry.get("position", ""),
                        "status":   entry.get("status", "").lower(),
                        "details":  entry.get("injuryType", ""),
                        "sport":    sport_key,
                        "source":   "rotowire",
                    })
            except Exception:
                pass

    return injuries


def fetch_injuries(sport_key: str) -> list[dict]:
    path = SPORT_PATHS.get(sport_key)
    if not path:
        return []

    html = get_html(f"{_BASE}{path}")
    if not html:
        return []

    return _parse_injury_table(html, sport_key)


def fetch_lineups(sport_key: str) -> list[dict]:
    """
    Fetch confirmed starting lineups. Critical for NBA and MLB same-day bets.
    An absent starter changes the entire value calculation.
    """
    path = LINEUP_PATHS.get(sport_key)
    if not path:
        return []

    html = get_html(f"{_BASE}{path}")
    if not html:
        return []

    lineups = []
    # Look for team lineup blocks
    team_pattern = re.compile(
        r'<div[^>]+team-name[^>]*>([^<]+)</div>.*?'
        r'(<ul[^>]+lineup[^>]*>.*?</ul>)',
        re.DOTALL | re.IGNORECASE,
    )
    player_pattern = re.compile(r'<li[^>]*>.*?<a[^>]*>([^<]+)</a>', re.DOTALL)

    for tm in team_pattern.finditer(html):
        team_name = tm.group(1).strip()
        players_html = tm.group(2)
        players = [m.group(1).strip() for m in player_pattern.finditer(players_html)]
        if players:
            lineups.append({
                "team":    team_name,
                "starters": players[:10],
                "sport":   sport_key,
                "source":  "rotowire",
            })

    return lineups
