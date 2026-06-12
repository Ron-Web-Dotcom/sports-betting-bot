"""
NBA / WNBA stats adapter.

Sources tried (in order of preference):
  1. NBA CDN  — cdn.nba.com/static/json/liveData/standings/00_standings.json (free, no key)
  2. SofaScore team search + last events (no key, but some endpoints 403 on VPS)
  3. Returns {} if all sources fail
"""
import logging

logger = logging.getLogger(__name__)

_NBA_CDN_STANDINGS = "https://cdn.nba.com/static/json/liveData/standings/00_standings.json"

_SOFASCORE_BASE = "https://api.sofascore.com/api/v1"
_SF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sofascore.com/",
}

# SofaScore tournament IDs
_TOURNAMENT_ID = {"nba": 132, "wnba": 536}


def _get_json(url: str, headers: dict | None = None) -> dict | list | None:
    from src.apis.base import get_json
    return get_json(url, headers=headers)


# ── NBA CDN ───────────────────────────────────────────────────────────────────

def _from_nba_cdn(team_name: str) -> dict:
    """Pull team record from NBA's official CDN standings JSON."""
    data = _get_json(_NBA_CDN_STANDINGS)
    if not data:
        return {}
    try:
        teams = data["resultSets"][0]["rowSet"]
        headers = data["resultSets"][0]["headers"]
    except (KeyError, IndexError, TypeError):
        return {}

    name_l = team_name.lower()
    for row in teams:
        record = dict(zip(headers, row))
        tname  = (record.get("TeamName") or "").lower()
        tcity  = (record.get("TeamCity") or "").lower()
        full   = f"{tcity} {tname}"
        if name_l in full or full in name_l or name_l in tname or tname in name_l:
            wins   = int(record.get("WINS", 0) or 0)
            losses = int(record.get("LOSSES", 0) or 0)
            streak = record.get("strCurrentStreak", "")
            l10    = record.get("L10", "")
            win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0
            return {
                "team":          f"{record.get('TeamCity','')} {record.get('TeamName','')}".strip(),
                "wins":          wins,
                "losses":        losses,
                "last_10_record": l10,
                "win_pct":       win_pct,
                "streak":        streak,
                "source":        "nba_cdn",
            }
    return {}


# ── SofaScore fallback ────────────────────────────────────────────────────────

def _sf_get(path: str) -> dict | list | None:
    return _get_json(f"{_SOFASCORE_BASE}{path}", headers=_SF_HEADERS)


def _from_sofascore(team_name: str, league: str) -> dict:
    """Search SofaScore for team, pull last events to compute W/L/streak."""
    from urllib.parse import quote
    data = _sf_get(f"/search/all/?q={quote(team_name)}")
    if not data or isinstance(data, list):
        return {}
    hits = (data.get("teams") or {}).get("hits", [])
    name_l = team_name.lower()
    team_entity = None
    for hit in hits:
        e = hit.get("entity", {})
        ename = (e.get("name") or "").lower()
        if name_l in ename or ename in name_l:
            team_entity = e
            break
    if not team_entity and hits:
        team_entity = hits[0].get("entity", {})
    if not team_entity:
        return {}

    team_id = str(team_entity.get("id", ""))
    if not team_id:
        return {}

    form_str, streak, wins, losses = _events_form(team_id)
    win_pct = round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0
    return {
        "team":          team_entity.get("name", team_name),
        "wins":          wins,
        "losses":        losses,
        "last_10_record": f"{wins}-{losses}",
        "win_pct":       win_pct,
        "streak":        streak,
        "form":          form_str,
        "source":        "sofascore_events",
    }


def _events_form(team_id: str, n: int = 10) -> tuple[str, str, int, int]:
    results = []
    for page in range(3):
        data = _sf_get(f"/team/{team_id}/events/last/{page}")
        events = (data or {}).get("events", []) if isinstance(data, dict) else []
        if not events:
            break
        for ev in events:
            hs  = (ev.get("homeScore") or {}).get("current")
            as_ = (ev.get("awayScore") or {}).get("current")
            if hs is None or as_ is None:
                continue
            is_home = str((ev.get("homeTeam") or {}).get("id", "")) == team_id
            won = (hs > as_) if is_home else (as_ > hs)
            results.append("W" if won else "L")
            if len(results) >= n:
                break
        if len(results) >= n:
            break

    if not results:
        return "", "", 0, 0

    form_str = "".join(results)
    wins   = form_str.count("W")
    losses = form_str.count("L")

    streak_char = results[0]
    count = sum(1 for r in results if r == streak_char) if all(r == streak_char for r in results) else next(
        (i for i, r in enumerate(results) if r != streak_char), len(results)
    )
    # simpler streak count
    count = 0
    for r in results:
        if r == streak_char:
            count += 1
        else:
            break
    streak = f"{streak_char}{count}"

    return form_str, streak, wins, losses


# ── Public API ────────────────────────────────────────────────────────────────

def get_team_recent_form(team_name: str, n: int = 10, league: str = "nba") -> dict:
    """
    Pull team record and recent form. NBA uses official CDN; WNBA uses SofaScore.
    """
    if league == "nba":
        result = _from_nba_cdn(team_name)
        if result:
            return result

    # WNBA or NBA CDN fallback
    result = _from_sofascore(team_name, league)
    if result:
        return result

    logger.debug("NBA stats: no form data found for '%s' (%s)", team_name, league)
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
