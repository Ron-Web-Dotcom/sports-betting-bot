"""
MLB Stats API adapter — free, no key required.
Base: https://statsapi.mlb.com/api/v1

Provides:
- Today's schedule with starting pitchers
- Team recent form (last 10 games)
- IL / injury list
- Head-to-head record
"""
import logging
import httpx
from src.core.timezone import et_now

logger = logging.getLogger(__name__)

_BASE    = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = 10.0
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _get(path: str, params: dict | None = None) -> dict | list | None:
    try:
        r = httpx.get(f"{_BASE}{path}", params=params, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("MLB Stats API error [%s]: %s", path, e)
        return None


# ── Team lookup ────────────────────────────────────────────────────────────────

_team_cache: dict[str, int] = {}

def _get_team_id(name: str) -> int | None:
    """Fuzzy match a team name to its MLB team ID."""
    global _team_cache
    if not _team_cache:
        data = _get("/teams", {"sportId": 1})
        if data:
            for t in data.get("teams", []):
                _team_cache[t["name"].lower()]         = t["id"]
                _team_cache[t["teamName"].lower()]     = t["id"]
                _team_cache[t.get("shortName","").lower()] = t["id"]
                _team_cache[t.get("abbreviation","").lower()] = t["id"]
    name_l = name.lower()
    if name_l in _team_cache:
        return _team_cache[name_l]
    for key, tid in _team_cache.items():
        if key and (name_l in key or key in name_l):
            return tid
    return None


# ── Today's schedule ───────────────────────────────────────────────────────────

def get_todays_games() -> list[dict]:
    """
    Return today's MLB schedule with starting pitchers for each game.
    Each dict: {game_pk, home_team, away_team, game_time, home_pitcher, away_pitcher, venue}
    """
    today = et_now().strftime("%Y-%m-%d")
    data  = _get("/schedule", {
        "sportId":    1,
        "date":       today,
        "hydrate":    "probablePitcher,team,venue",
        "gameType":   "R,F,D,L,W",
    })
    if not data:
        return []

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            home_pitcher = home.get("probablePitcher", {}).get("fullName", "TBD")
            away_pitcher = away.get("probablePitcher", {}).get("fullName", "TBD")
            games.append({
                "game_pk":      g.get("gamePk"),
                "home_team":    home.get("team", {}).get("name", ""),
                "away_team":    away.get("team", {}).get("name", ""),
                "game_time":    g.get("gameDate", ""),
                "venue":        g.get("venue", {}).get("name", ""),
                "home_pitcher": home_pitcher,
                "away_pitcher": away_pitcher,
                "status":       g.get("status", {}).get("detailedState", ""),
            })
    return games


# ── Recent form ────────────────────────────────────────────────────────────────

def get_team_recent_form(team_name: str, n: int = 10) -> dict:
    """
    Return a team's last N game results.
    Returns: {wins, losses, streak, last_n_games: [...], win_pct}
    """
    team_id = _get_team_id(team_name)
    if not team_id:
        return {}

    data = _get(f"/teams/{team_id}/records", {"leagueId": "103,104"})
    record = {}
    if data:
        for rec in data.get("records", []):
            for tr in rec.get("teamRecords", []):
                if tr.get("team", {}).get("id") == team_id:
                    record = {
                        "wins":    tr.get("wins", 0),
                        "losses":  tr.get("losses", 0),
                        "win_pct": float(tr.get("winningPercentage", 0)),
                        "streak":  tr.get("streak", {}).get("streakCode", ""),
                        "home":    tr.get("records", {}).get("splitRecords", []),
                    }
                    break

    # Last N games from schedule
    today    = et_now()
    sched    = _get("/schedule", {
        "teamId":   team_id,
        "sportId":  1,
        "startDate": today.strftime("%Y-%m-01"),
        "endDate":   today.strftime("%Y-%m-%d"),
        "gameType": "R",
        "hydrate":  "decisions",
    })
    results = []
    if sched:
        for date_entry in sched.get("dates", []):
            for g in date_entry.get("games", []):
                status = g.get("status", {}).get("detailedState", "")
                if status != "Final":
                    continue
                home   = g["teams"]["home"]
                away   = g["teams"]["away"]
                is_home = home.get("team", {}).get("id") == team_id
                our    = home if is_home else away
                their  = away if is_home else home
                w = our.get("score", 0)
                l = their.get("score", 0)
                results.append({
                    "date":     g.get("gameDate", "")[:10],
                    "result":   "W" if w > l else "L",
                    "score":    f"{w}-{l}",
                    "opponent": their.get("team", {}).get("name", ""),
                    "home":     is_home,
                })
    last_n = results[-n:]
    wins   = sum(1 for r in last_n if r["result"] == "W")

    return {
        **record,
        f"last_{n}_games": last_n,
        f"last_{n}_record": f"{wins}-{len(last_n)-wins}",
    }


# ── IL / injury list ───────────────────────────────────────────────────────────

def get_team_injuries(team_name: str) -> list[dict]:
    """
    Return players currently on the Injured List for a team.
    Each dict: {player, injury, il_type (10-day / 60-day), since}
    """
    team_id = _get_team_id(team_name)
    if not team_id:
        return []

    data = _get(f"/teams/{team_id}/roster", {"rosterType": "injuries"})
    if not data:
        return []

    injuries = []
    for entry in data.get("roster", []):
        person = entry.get("person", {})
        status = entry.get("status", {})
        injuries.append({
            "player":   person.get("fullName", ""),
            "position": entry.get("position", {}).get("abbreviation", ""),
            "status":   status.get("description", "IL"),
            "il_type":  status.get("code", ""),
        })
    return injuries


# ── Pitcher stats ─────────────────────────────────────────────────────────────

def get_pitcher_stats(pitcher_name: str) -> dict:
    """
    Return season stats + last 3 starts for a pitcher.
    ERA, WHIP, K/9, BB/9, innings pitched, record.
    """
    # Search for player ID
    search = _get("/people/search", {"names": pitcher_name, "sportId": 1})
    if not search:
        return {}
    people = search.get("people", [])
    if not people:
        return {}
    person_id = people[0].get("id")
    if not person_id:
        return {}

    # Season stats
    stats_data = _get(f"/people/{person_id}/stats", {
        "stats": "season,gameLog",
        "group": "pitching",
        "season": et_now().year,
        "limit": 3,  # last 3 game log entries
    })
    if not stats_data:
        return {"name": pitcher_name}

    season_stats = {}
    last_3_starts = []
    for stat_group in stats_data.get("stats", []):
        if stat_group.get("type", {}).get("displayName") == "season":
            splits = stat_group.get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                season_stats = {
                    "era":   s.get("era", "N/A"),
                    "whip":  s.get("whip", "N/A"),
                    "wins":  s.get("wins", 0),
                    "losses": s.get("losses", 0),
                    "innings_pitched": s.get("inningsPitched", "0"),
                    "strikeouts": s.get("strikeOuts", 0),
                    "walks": s.get("baseOnBalls", 0),
                }
        elif stat_group.get("type", {}).get("displayName") == "gameLog":
            for split in stat_group.get("splits", [])[:3]:
                s = split.get("stat", {})
                last_3_starts.append({
                    "date":    split.get("date", ""),
                    "opponent": split.get("opponent", {}).get("name", ""),
                    "result":  f"{s.get('wins',0)}-{s.get('losses',0)}",
                    "era_game": s.get("era", ""),
                    "ip":      s.get("inningsPitched", ""),
                    "er":      s.get("earnedRuns", 0),
                    "k":       s.get("strikeOuts", 0),
                })

    return {
        "name":          pitcher_name,
        "season_stats":  season_stats,
        "last_3_starts": last_3_starts,
    }


# ── Starting pitchers for a matchup ───────────────────────────────────────────

def get_starting_pitchers(home_team: str, away_team: str) -> dict:
    """
    Return probable starters for today's matchup between home_team and away_team.
    Returns: {home_pitcher, away_pitcher, game_pk, venue}
    """
    games = get_todays_games()
    home_l = home_team.lower()
    away_l = away_team.lower()
    for g in games:
        gh = g["home_team"].lower()
        ga = g["away_team"].lower()
        if (home_l in gh or gh in home_l) and (away_l in ga or ga in away_l):
            return {
                "home_pitcher": g["home_pitcher"],
                "away_pitcher": g["away_pitcher"],
                "venue":        g["venue"],
                "game_time":    g["game_time"],
            }
    return {}


# ── Main entry point for data_hub ─────────────────────────────────────────────

def enrich_game_context(home_team: str, away_team: str) -> dict:
    """
    Pull all available MLB context for a matchup.
    Called by data_hub for baseball_mlb games.
    Returns a dict ready to be merged into game_context.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    pitchers = get_starting_pitchers(home_team, away_team)

    # Fetch form, injuries, and pitcher stats in parallel
    tasks = {
        "home_form":      (get_team_recent_form,  (home_team,)),
        "away_form":      (get_team_recent_form,  (away_team,)),
        "home_injuries":  (get_team_injuries,     (home_team,)),
        "away_injuries":  (get_team_injuries,     (away_team,)),
    }
    if pitchers.get("home_pitcher") and pitchers["home_pitcher"] != "TBD":
        tasks["home_pitcher_stats"] = (get_pitcher_stats, (pitchers["home_pitcher"],))
    if pitchers.get("away_pitcher") and pitchers["away_pitcher"] != "TBD":
        tasks["away_pitcher_stats"] = (get_pitcher_stats, (pitchers["away_pitcher"],))

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, args) in tasks.items()}
        for future in _as_completed(futures, timeout=15):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                pass

    return {
        "mlb_pitchers":           pitchers,
        "mlb_home_pitcher_stats": results.get("home_pitcher_stats", {}),
        "mlb_away_pitcher_stats": results.get("away_pitcher_stats", {}),
        "mlb_home_form":          results.get("home_form", {}),
        "mlb_away_form":          results.get("away_form", {}),
        "mlb_home_injuries":      results.get("home_injuries", []),
        "mlb_away_injuries":      results.get("away_injuries", []),
    }
