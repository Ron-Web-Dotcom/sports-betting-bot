"""
NBA / WNBA stats adapter.

Uses Ball Don't Lie (balldontlie.io) — free, works from VPS, no key needed.
stats.nba.com blocks datacenter IPs (403 WAF), so we use BDL instead.

Provides:
- Team recent form (last 10 games W-L, streak)
- Today's games / scores
- Season standings context
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_BDL_BASE = "https://api.balldontlie.io/v1"


def _get(path: str, params: dict | None = None) -> dict | None:
    from src.apis.base import get_json
    return get_json(f"{_BDL_BASE}{path}", params=params)


# ── Team lookup ────────────────────────────────────────────────────────────────

_team_cache: dict[str, dict] = {}

def _get_team(name: str) -> dict | None:
    global _team_cache
    if not _team_cache:
        data = _get("/teams", {"per_page": 100})
        if data:
            for t in data.get("data", []):
                full = t.get("full_name", "").lower()
                city = t.get("city", "").lower()
                nick = t.get("name", "").lower()
                abbr = t.get("abbreviation", "").lower()
                for key in (full, city, nick, abbr):
                    if key:
                        _team_cache[key] = t
    name_l = name.lower()
    if name_l in _team_cache:
        return _team_cache[name_l]
    for key, team in _team_cache.items():
        if name_l in key or key in name_l:
            return team
    return None


# ── Recent form ────────────────────────────────────────────────────────────────

def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Last N completed games for a team — W-L record, streak, recent results.
    Works for NBA. WNBA not covered by Ball Don't Lie — falls back to ESPN.
    """
    if league == "wnba":
        return _wnba_form_espn(team_name, n)

    team = _get_team(team_name)
    if not team:
        return {}
    team_id = team.get("id")

    # Fetch last N completed games
    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=60)).strftime("%Y-%m-%d")
    end   = today.strftime("%Y-%m-%d")

    data = _get("/games", {
        "team_ids[]": team_id,
        "start_date": start,
        "end_date":   end,
        "per_page":   50,
        "seasons[]":  2025,   # 2025-26 season
    })
    if not data:
        return {}

    games = [g for g in data.get("data", []) if g.get("status") == "Final"]
    games = sorted(games, key=lambda g: g.get("date", ""), reverse=True)[:n]

    if not games:
        return {}

    results = []
    for g in games:
        is_home  = g.get("home_team", {}).get("id") == team_id
        our_score  = g.get("home_team_score" if is_home else "visitor_team_score", 0) or 0
        opp_score  = g.get("visitor_team_score" if is_home else "home_team_score", 0) or 0
        opp_name   = g.get("visitor_team" if is_home else "home_team", {}).get("full_name", "")
        result     = "W" if our_score > opp_score else "L"
        results.append({
            "date":     g.get("date", "")[:10],
            "result":   result,
            "score":    f"{our_score}-{opp_score}",
            "opponent": opp_name,
            "home":     is_home,
        })

    wins   = sum(1 for r in results if r["result"] == "W")
    losses = len(results) - wins

    # Calculate streak
    streak_type  = results[0]["result"] if results else ""
    streak_count = 0
    for r in results:
        if r["result"] == streak_type:
            streak_count += 1
        else:
            break

    return {
        f"last_{n}_record": f"{wins}-{losses}",
        "wins":         wins,
        "losses":       losses,
        "streak":       f"{streak_type}{streak_count}",
        "recent_games": results[:5],
    }


def _wnba_form_espn(team_name: str, n: int = 10) -> dict:
    """Fallback: pull WNBA team record from ESPN scoreboard."""
    try:
        from src.apis.espn import fetch_scoreboard
        board = fetch_scoreboard("basketball_wnba") or []
        for game in board:
            for side in ("home", "away"):
                if team_name.lower() in game.get(f"{side}_team", "").lower():
                    return {
                        "source": "espn",
                        "note": f"WNBA form via ESPN — {game.get(f'{side}_team')}",
                    }
    except Exception:
        pass
    return {}


# ── Today's NBA/WNBA games ────────────────────────────────────────────────────

def get_todays_games(league: str = "nba") -> list[dict]:
    """Return today's games from Ball Don't Lie (NBA only)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data  = _get("/games", {"dates[]": today, "per_page": 50})
    if not data:
        return []
    return [
        {
            "home_team":  g.get("home_team", {}).get("full_name", ""),
            "away_team":  g.get("visitor_team", {}).get("full_name", ""),
            "status":     g.get("status", ""),
            "home_score": g.get("home_team_score"),
            "away_score": g.get("visitor_team_score"),
        }
        for g in data.get("data", [])
    ]


# ── Main entry point ───────────────────────────────────────────────────────────

def enrich_game_context(home_team: str, away_team: str, league: str = "nba") -> dict:
    """Pull NBA/WNBA context for a matchup — form for both teams."""
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
