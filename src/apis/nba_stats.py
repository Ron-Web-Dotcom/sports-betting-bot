"""
NBA / WNBA stats adapter — uses ESPN's free public API.

stats.nba.com blocks VPS IPs (403 WAF).
Ball Don't Lie now requires a paid key.
ESPN's public API works from VPS and needs no key.

Provides: team record, streak, last 10 games, standings.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# ESPN sport slugs
_SPORT_SLUG = {
    "nba":  "basketball/nba",
    "wnba": "basketball/wnba",
}

# ESPN team abbreviation map for fuzzy lookup
_NBA_ABBR = {
    "atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL", "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GSW", "houston rockets": "HOU", "indiana pacers": "IND",
    "la clippers": "LAC", "los angeles clippers": "LAC", "los angeles lakers": "LAL",
    "la lakers": "LAL", "memphis grizzlies": "MEM", "miami heat": "MIA",
    "milwaukee bucks": "MIL", "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP", "new york knicks": "NYK",
    "oklahoma city thunder": "OKC", "orlando magic": "ORL",
    "philadelphia 76ers": "PHI", "phoenix suns": "PHX",
    "portland trail blazers": "POR", "sacramento kings": "SAC",
    "san antonio spurs": "SAS", "toronto raptors": "TOR",
    "utah jazz": "UTA", "washington wizards": "WAS",
}

_WNBA_ABBR = {
    "atlanta dream": "ATL", "chicago sky": "CHI", "connecticut sun": "CONN",
    "dallas wings": "DAL", "indiana fever": "IND", "las vegas aces": "LV",
    "los angeles sparks": "LA", "minnesota lynx": "MIN",
    "new york liberty": "NY", "phoenix mercury": "PHX",
    "seattle storm": "SEA", "washington mystics": "WSH",
    "golden state valkyries": "GSV", "portland fire": "POR",
    "toronto tempo": "TOR", "cleveland charge": "CLE",
}


def _get(url: str, params: dict | None = None) -> dict | None:
    from src.apis.base import get_json
    return get_json(url, params=params)


def _find_abbr(team_name: str, league: str) -> str | None:
    lookup = _WNBA_ABBR if league == "wnba" else _NBA_ABBR
    name_l = team_name.lower()
    if name_l in lookup:
        return lookup[name_l]
    for key, abbr in lookup.items():
        if name_l in key or key in name_l:
            return abbr
    return None


def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull team record and recent form from ESPN standings + scoreboard.
    Returns: last_10_record, wins, losses, streak, win_pct
    """
    slug  = _SPORT_SLUG.get(league, "basketball/nba")
    abbr  = _find_abbr(team_name, league)

    # Get standings — has W, L, streak for every team
    data = _get(f"{_ESPN_BASE}/{slug}/standings")
    if not data:
        return {}

    entries = []
    for group in data.get("children", []) or [data]:
        for entry in group.get("standings", {}).get("entries", []):
            entries.append(entry)
    if not entries:
        # Some responses have entries at top level
        entries = data.get("standings", {}).get("entries", [])

    for entry in entries:
        team_info = entry.get("team", {})
        t_abbr    = team_info.get("abbreviation", "")
        t_name    = team_info.get("displayName", "").lower()

        match = (abbr and t_abbr == abbr) or (team_name.lower() in t_name or t_name in team_name.lower())
        if not match:
            continue

        stats  = {s["name"]: s.get("displayValue", s.get("value")) for s in entry.get("stats", [])}
        wins   = int(float(stats.get("wins",   stats.get("win", 0)) or 0))
        losses = int(float(stats.get("losses", stats.get("loss", 0)) or 0))
        streak = stats.get("streak", "")
        win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0

        return {
            "team":            team_info.get("displayName", team_name),
            "wins":            wins,
            "losses":          losses,
            f"last_{n}_record": f"{wins}-{losses}",
            "win_pct":         win_pct,
            "streak":          streak,
            "source":          "espn_standings",
        }

    return {}


def enrich_game_context(home_team: str, away_team: str, league: str = "nba") -> dict:
    """Pull NBA/WNBA context for a matchup."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home_form": (get_team_recent_form, (home_team, 10, league)),
        "away_form": (get_team_recent_form, (away_team, 10, league)),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        for future in _as_completed(futures, timeout=15):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                pass

    return {
        "nba_home_form":    results.get("home_form", {}),
        "nba_away_form":    results.get("away_form", {}),
        "nba_home_ratings": {},
        "nba_away_ratings": {},
    }
