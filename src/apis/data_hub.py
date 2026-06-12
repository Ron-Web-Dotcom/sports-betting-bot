"""
Data Hub — single interface for all real-world data sources.

Aggregates ESPN, StatMuse, Ball Don't Lie, Sleeper, Weather, Action Network,
RotoWire, SofaScore, SportsData.io, PrizePicks, and Underdog Fantasy into one
normalized context payload.

Free sources: ESPN, StatMuse, SofaScore, Action Network, Sleeper, RotoWire,
              Ball Don't Lie, PrizePicks (public), Underdog (public)
Premium (key required): SportsData.io (single universal key)

This payload is what gets fed to the AI engine and pick gate.
Richer context = better explanations = more trustworthy recommendations.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

logger = logging.getLogger(__name__)


def build_game_context(
    sport_key:  str,
    home_team:  str,
    away_team:  str,
    game_time:  str,           # ISO datetime string
    venue:      str = "",
    home_key:   str = "",      # ESPN team ID (optional)
    away_key:   str = "",
) -> dict:
    """
    Fetch all available real-world data for a game in parallel.
    Returns a unified context dict consumed by analyse_pick() and the pick gate.

    Failures from any individual source are caught and logged — the hub
    degrades gracefully rather than blocking pick generation.
    """
    context: dict = {
        "sport":     sport_key,
        "home_team": home_team,
        "away_team": away_team,
        "game_time": game_time,
        "venue":     venue,
        "sources_used":   [],
        "sources_failed": [],
    }

    tasks = {
        "injuries_espn_home":  (_fetch_injuries_espn,   (sport_key,)),
        "news_espn":           (_fetch_news_espn,        (sport_key,)),
        "scoreboard":          (_fetch_scoreboard_espn,  (sport_key,)),
        # StatMuse disabled — VPS datacenter IPs return 403; Sofascore + Sportradar cover H2H/form
        "sharp_action":        (_fetch_sharp_action,     (sport_key, home_team, away_team)),
        "weather":             (_fetch_weather,           (venue or home_team, game_time, sport_key)),
        "trending_players":    (_fetch_trending,          (sport_key,)),
        "sofascore":           (_fetch_sofascore_game,    (sport_key, home_team, away_team, game_time)),
        # SportsData.io — checks for its own API key, returns {} if unconfigured
        "sportsdataio":        (_fetch_sportsdataio,      (sport_key, home_team, away_team)),
        # Exchange / prediction markets
        "kalshi_markets":      (_fetch_kalshi_markets,    (sport_key,)),
        # Sportradar — real H2H, recent form, injuries (requires SPORTRADAR_API_KEY in .env)
        "sportradar":          (_fetch_sportradar_game,   (sport_key, home_team, away_team)),
    }

    # NBA-only: Ball Don't Lie for deeper player stats
    if sport_key == "basketball_nba":
        tasks["nba_stats"] = (_fetch_nba_stats, (home_team, away_team))

    # NFL/NBA: Sleeper for real-time injury status
    if sport_key in ("americanfootball_nfl", "basketball_nba"):
        tasks["sleeper_injuries"] = (_fetch_sleeper_injuries, (sport_key,))
        tasks["rotowire_injuries"] = (_fetch_rotowire_injuries, (sport_key,))

    # Run all fetches in parallel — max 4 workers to stay within 1GB VPS RAM
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        try:
            for future in as_completed(futures, timeout=30):
                key = futures[future]
                try:
                    result = future.result()
                    if result:
                        context[key] = result
                        context["sources_used"].append(key.split("_")[0])
                except Exception as e:
                    logger.warning("Data hub fetch failed [%s]: %s", key, e)
                    context["sources_failed"].append(key)
        except TimeoutError:
            logger.warning("Data hub timed out after 30s; cancelling remaining fetches")
            for f in futures:
                f.cancel()
            context["sources_failed"].extend(
                key for f, key in futures.items() if not f.done()
            )

    context["sources_used"] = list(set(context["sources_used"]))
    context["data_completeness"] = _score_completeness(context)

    logger.info(
        "Game context built [%s vs %s]: %d sources, completeness %.0f%%",
        home_team, away_team,
        len(context["sources_used"]),
        context["data_completeness"] * 100,
    )
    return context


def build_player_context(
    player_name: str,
    sport_key:   str,
    opponent:    str = "",
    n_games:     int = 5,
) -> dict:
    """
    Assemble player context for prop bet analysis.
    Pulls recent form, season stats, injury status, and vs-opponent splits.
    """
    context: dict = {
        "player":  player_name,
        "sport":   sport_key,
        "opponent": opponent,
        "sources_used": [],
    }

    tasks = {
        "season_stats":  (_fetch_player_season, (player_name, sport_key)),
        "recent_form":   (_fetch_player_recent, (player_name, sport_key, n_games)),
    }
    if opponent:
        tasks["vs_opponent"] = (_fetch_player_vs_team, (player_name, opponent, sport_key))
    if sport_key == "basketball_nba":
        tasks["bdl_log"] = (_fetch_bdl_game_log, (player_name,))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        try:
            for future in as_completed(futures, timeout=20):
                key = futures[future]
                try:
                    result = future.result()
                    if result:
                        context[key] = result
                        context["sources_used"].append(key.split("_")[0])
                except Exception as e:
                    logger.warning("Player context fetch failed [%s]: %s", key, e)
        except TimeoutError:
            logger.warning("Player context timed out after 20s; cancelling remaining fetches")
            for f in futures:
                f.cancel()

    context["sources_used"] = list(set(context["sources_used"]))
    return context


# ── Private fetchers (called by ThreadPoolExecutor) ────────────────────────────

def _fetch_injuries_espn(sport_key: str) -> list:
    from src.apis.espn import fetch_injuries
    return fetch_injuries(sport_key)

def _fetch_news_espn(sport_key: str) -> list:
    from src.apis.espn import fetch_news
    return fetch_news(sport_key, limit=5)

def _fetch_scoreboard_espn(sport_key: str) -> list:
    from src.apis.espn import fetch_scoreboard
    return fetch_scoreboard(sport_key)


def _fetch_sharp_action(sport_key: str, home: str, away: str) -> dict:
    # Action Network API is defunct — returns 404 on all endpoints
    return {}

def _fetch_weather(venue: str, game_time: str, sport_key: str) -> dict:
    from src.apis.weather import get_game_weather, WEATHER_RELEVANT
    if sport_key not in WEATHER_RELEVANT:
        return {}
    return get_game_weather(venue, game_time)

def _fetch_trending(sport_key: str) -> list:
    from src.apis.sleeper import get_trending_players
    return get_trending_players(sport_key, trend_type="add", limit=10)

def _fetch_nba_stats(home: str, away: str) -> dict:
    from src.apis.balldontlie import get_teams
    teams = get_teams()
    result = {}
    for t in teams:
        if home.lower() in t.get("name", "").lower():
            result["home_team"] = t
        if away.lower() in t.get("name", "").lower():
            result["away_team"] = t
    return result

def _fetch_sleeper_injuries(sport_key: str) -> list:
    from src.apis.sleeper import get_trending_players
    return get_trending_players(sport_key, trend_type="drop", limit=15)

def _fetch_rotowire_injuries(sport_key: str) -> list:
    from src.apis.rotowire import fetch_injuries
    return fetch_injuries(sport_key)

def _fetch_sofascore_game(sport_key: str, home_team: str, away_team: str, game_time: str) -> dict:
    from src.apis.sofascore import enrich_game_context
    result = enrich_game_context(sport_key, home_team, away_team, game_time)
    return result if result.get("available") else {}

def _fetch_sportsdataio(sport_key: str, home_team: str, away_team: str) -> dict:
    from src.apis.sportsdataio import enrich_game_context
    result = enrich_game_context(sport_key, home_team, away_team)
    return result if result.get("available") else {}

def _fetch_kalshi_markets(sport_key: str) -> list:
    from src.apis.kalshi import get_markets
    return get_markets(sport_key, limit=50)

def _fetch_sportradar_game(sport_key: str, home_team: str, away_team: str) -> dict:
    from src.apis.sportradar import enrich_game_context
    result = enrich_game_context(sport_key, home_team, away_team)
    return result if result.get("available") else {}

def _fetch_prizepicks_props(sport_key: str, home_team: str, away_team: str) -> list:
    from src.apis.prizepicks import get_projections
    props = get_projections(sport_key)
    # Filter to players on either team in this game
    h, a = home_team.lower(), away_team.lower()
    return [
        p for p in props
        if h in (p.get("team") or "").lower()
        or a in (p.get("team") or "").lower()
        or h in (p.get("opponent") or "").lower()
        or a in (p.get("opponent") or "").lower()
    ] or props  # fallback: return all if no team match (team names may differ)

def _fetch_underdog_props(sport_key: str, home_team: str, away_team: str) -> list:
    from src.apis.underdog import get_over_under_lines
    props = get_over_under_lines(sport_key)
    h, a = home_team.lower(), away_team.lower()
    return [
        p for p in props
        if h in (p.get("team") or "").lower()
        or a in (p.get("team") or "").lower()
        or h in (p.get("opponent") or "").lower()
        or a in (p.get("opponent") or "").lower()
    ] or props


def _fetch_bdl_game_log(name: str) -> list:
    from src.apis.balldontlie import search_player, player_game_log
    from datetime import datetime
    players = search_player(name)
    if not players:
        return []
    pid = players[0].get("id")
    return player_game_log(pid, last_n=10)


def _score_completeness(context: dict) -> float:
    """Score how complete the data context is (0.0–1.0).

    Core sources (always expected) count double; premium sources (key-gated)
    count as bonus so missing keys don't tank the score.
    """
    core = [
        bool(context.get("injuries_espn_home")),
        bool(context.get("sharp_action")),
        bool(context.get("news_espn")),
        bool(context.get("sofascore")),
    ]
    # Bonus sources (each adds depth without penalising missing keys)
    bonus = 0.0
    if context.get("sportradar"):       bonus += 0.20  # real H2H, form, injuries — major boost
    if context.get("sportsdataio"):     bonus += 0.10  # standings + injuries + team stats
    if context.get("kalshi_markets"):   bonus += 0.03  # prediction market consensus
    core_score = sum(core) / len(core)
    return min(1.0, round(core_score + bonus, 4))
