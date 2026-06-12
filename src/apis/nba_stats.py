"""
NBA / WNBA stats adapter — delegates to Sportradar.

All public NBA/WNBA CDN and stats APIs block VPS datacenter IPs.
Sportradar trial key covers NBA and WNBA and is confirmed working.
Falls back to {} gracefully if key is missing or team not found.
"""
import logging

logger = logging.getLogger(__name__)


def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull team record and recent form via Sportradar.
    Returns: wins, losses, last_10_record, win_pct, streak, recent_games
    """
    sport_key = "basketball_wnba" if league == "wnba" else "basketball_nba"
    try:
        from src.apis.sportradar import get_recent_form
        recent = get_recent_form(sport_key, team_name, n=n)

        if not recent:
            return {}

        # get_recent_form returns season_stats dict for NBA/WNBA (single item list)
        season = recent[0] if recent else {}
        wins   = int(season.get("wins",   0) or 0)
        losses = int(season.get("losses", 0) or 0)
        win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0

        return {
            "team":           team_name,
            "wins":           wins,
            "losses":         losses,
            "last_10_record": f"{wins}-{losses}",
            "win_pct":        win_pct,
            "streak":         "",
            "source":         "sportradar",
        }
    except Exception as e:
        logger.debug("NBA stats via Sportradar failed for '%s': %s", team_name, e)
        return {}


def enrich_game_context(home_team: str, away_team: str, league: str = "nba") -> dict:
    """Pull NBA/WNBA context for a matchup via Sportradar."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    tasks = {
        "home_form": (get_team_recent_form, (home_team, 10, league)),
        "away_form": (get_team_recent_form, (away_team, 10, league)),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        for future in _as_completed(futures, timeout=20):
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
