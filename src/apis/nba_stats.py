"""
NBA / WNBA stats adapter.

NBA  — Sportradar (trial key covers NBA)
WNBA — TheSportsDB (free, no key, VPS-friendly, confirmed working)

TheSportsDB WNBA: league ID 4516, 10 teams with IDs, last events per team.
"""
import logging

logger = logging.getLogger(__name__)

_TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# TheSportsDB WNBA team name → team ID (populated on first use)
_wnba_team_cache: dict[str, str] = {}


def _get_json(url: str) -> dict | None:
    from src.apis.base import get_json
    return get_json(url)


def _load_wnba_teams() -> dict[str, str]:
    """Return {team_name_lower: team_id} for all WNBA teams."""
    global _wnba_team_cache
    if _wnba_team_cache:
        return _wnba_team_cache
    data = _get_json(f"{_TSDB_BASE}/search_all_teams.php?l=WNBA")
    if not data:
        return {}
    for t in (data.get("teams") or []):
        name = (t.get("strTeam") or "").lower()
        tid  = t.get("idTeam", "")
        if name and tid:
            _wnba_team_cache[name] = str(tid)
    logger.debug("TheSportsDB WNBA teams loaded: %d", len(_wnba_team_cache))
    return _wnba_team_cache


def _find_wnba_team_id(team_name: str) -> str | None:
    teams = _load_wnba_teams()
    name_l = team_name.lower()
    # Exact match
    if name_l in teams:
        return teams[name_l]
    # Substring / last-word match
    for tname, tid in teams.items():
        if name_l in tname or tname in name_l:
            return tid
        last = name_l.split()[-1] if name_l.split() else ""
        if last and len(last) > 3 and last in tname:
            return tid
    # Fallback: search endpoint (catches teams missing from search_all_teams)
    from urllib.parse import quote
    data = _get_json(f"{_TSDB_BASE}/searchteams.php?t={quote(team_name)}")
    for t in (data or {}).get("teams") or []:
        league = (t.get("strLeague") or "").upper()
        if "WNBA" in league:
            tid = str(t.get("idTeam", ""))
            tname = (t.get("strTeam") or "").lower()
            if tid:
                _wnba_team_cache[tname] = tid  # cache for next call
                return tid
    return None


def _wnba_form(team_name: str, n: int = 10) -> dict:
    """Pull WNBA team recent form from TheSportsDB."""
    team_id = _find_wnba_team_id(team_name)
    if not team_id:
        logger.debug("TheSportsDB: WNBA team '%s' not found", team_name)
        return {}

    data = _get_json(f"{_TSDB_BASE}/eventslast.php?id={team_id}")
    if not data:
        return {}

    events = data.get("results") or []
    results = []
    wins = losses = 0

    for ev in events[:n]:
        home = (ev.get("strHomeTeam") or "").lower()
        away = (ev.get("strAwayTeam") or "").lower()
        hs   = ev.get("intHomeScore")
        as_  = ev.get("intAwayScore")
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (ValueError, TypeError):
            continue
        name_l = team_name.lower()
        is_home = name_l in home or any(w in home for w in name_l.split())
        won = (hs > as_) if is_home else (as_ > hs)
        results.append("W" if won else "L")
        if won:
            wins += 1
        else:
            losses += 1

    if not results:
        return {}

    form_str = "".join(results)
    streak_char = results[0]
    streak_cnt = 0
    for r in results:
        if r == streak_char:
            streak_cnt += 1
        else:
            break

    win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0
    return {
        "team":           team_name,
        "wins":           wins,
        "losses":         losses,
        "last_10_record": f"{wins}-{losses}",
        "win_pct":        win_pct,
        "streak":         f"{streak_char}{streak_cnt}",
        "form":           form_str,
        "source":         "thesportsdb",
    }


def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull team record and recent form.
    WNBA → TheSportsDB (free, VPS-friendly)
    NBA  → Sportradar (trial key covers NBA)
    """
    if league == "wnba":
        result = _wnba_form(team_name, n)
        if result:
            return result
        logger.debug("WNBA form not found for '%s'", team_name)
        return {}

    # NBA via Sportradar
    sport_key = "basketball_nba"
    try:
        from src.apis.sportradar import get_recent_form
        recent = get_recent_form(sport_key, team_name, n=n)
        if not recent:
            return {}
        season  = recent[0] if recent else {}
        wins    = int(season.get("wins",   0) or 0)
        losses  = int(season.get("losses", 0) or 0)
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
    """Pull NBA/WNBA context for a matchup."""
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
