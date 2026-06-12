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


def _extract_entries(data: dict) -> list:
    """Extract standings entries from ESPN API response (handles multiple structures)."""
    entries = []

    # Structure 1: data.children[].standings.entries[]
    for group in data.get("children", []):
        for sub in group.get("children", []) or [group]:
            for e in sub.get("standings", {}).get("entries", []):
                entries.append(e)
        # Also try direct standings on group
        for e in group.get("standings", {}).get("entries", []):
            entries.append(e)

    # Structure 2: data.standings.entries[]
    if not entries:
        entries = data.get("standings", {}).get("entries", [])

    # Structure 3: data.entries[]
    if not entries:
        entries = data.get("entries", [])

    return entries


def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull team record and recent form from ESPN standings.
    Returns: last_10_record, wins, losses, streak, win_pct
    """
    slug = _SPORT_SLUG.get(league, "basketball/nba")
    abbr = _find_abbr(team_name, league)

    data = _get(f"{_ESPN_BASE}/{slug}/standings")
    if not data:
        return {}

    entries = _extract_entries(data)
    if not entries:
        logger.debug("NBA stats: no entries found in ESPN standings response for %s", league)
        return {}

    name_l = team_name.lower()
    for entry in entries:
        team_info = entry.get("team", {})
        t_abbr = team_info.get("abbreviation", "")
        t_name = team_info.get("displayName", "").lower()
        t_short = team_info.get("shortDisplayName", "").lower()
        t_nick  = team_info.get("name", "").lower()

        match = (
            (abbr and t_abbr.upper() == abbr.upper()) or
            (name_l in t_name) or (t_name in name_l) or
            (name_l in t_short) or (t_short in name_l) or
            (t_nick and t_nick in name_l)
        )
        if not match:
            continue

        # ESPN stats array — field names vary by season/endpoint
        stats: dict = {}
        for s in entry.get("stats", []):
            sname = s.get("name", "")
            sval  = s.get("displayValue") or s.get("value")
            stats[sname] = sval

        # Win/loss — ESPN uses several names
        wins   = int(float(stats.get("wins")   or stats.get("win")    or stats.get("overall_wins")  or 0))
        losses = int(float(stats.get("losses") or stats.get("loss")   or stats.get("overall_losses") or 0))

        # Streak — may be "streak" or "streakDescription"
        streak = stats.get("streak") or stats.get("streakDescription") or ""

        win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0

        return {
            "team":             team_info.get("displayName", team_name),
            "wins":             wins,
            "losses":           losses,
            f"last_{n}_record": f"{wins}-{losses}",
            "win_pct":          win_pct,
            "streak":           streak,
            "source":           "espn_standings",
        }

    logger.debug("NBA stats: team '%s' not found in ESPN standings (%d entries)", team_name, len(entries))
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
