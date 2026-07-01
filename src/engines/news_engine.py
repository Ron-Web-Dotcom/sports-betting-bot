"""
Engine 10 — News Intelligence Engine.

Continuously monitors: injuries, suspensions, trades, lineups,
coaching changes, weather, breaking news.

On each update, re-evaluates affected picks and triggers alerts.
"""
import logging
import httpx
from src.core.config import ESPN_API_BASE, SPORTS
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

INJURY_CRITICAL_STATUSES = {"out", "doubtful", "questionable", "ir", "gtd"}


# ── ESPN data pulls ────────────────────────────────────────────────────────────

def _espn_get(path: str) -> dict | None:
    try:
        r = httpx.get(f"{ESPN_API_BASE}/{path}", timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        logger.error("ESPN error %s: %s", path, e)
        return None


def fetch_injuries(sport_key: str) -> list[dict]:
    mapping = {
        "americanfootball_nfl":   ("football",   "nfl"),
        "americanfootball_ncaaf": ("football",   "college-football"),
        "basketball_nba":         ("basketball", "nba"),
        "basketball_ncaab":       ("basketball", "mens-college-basketball"),
        "baseball_mlb":           ("baseball",   "mlb"),
        "icehockey_nhl":          ("hockey",     "nhl"),
        "soccer_epl":             ("soccer",     "eng.1"),
        "soccer_fifa_world_cup":  ("soccer",     "fifa.world"),
    }
    mapping_entry = mapping.get(sport_key)
    if not mapping_entry:
        return []
    sport, league = mapping_entry
    data = _espn_get(f"{sport}/{league}/injuries")
    if not data:
        return []

    injuries = []
    for item in data.get("injuries", []):
        athlete = item.get("athlete", {})
        injuries.append({
            "sport":       sport_key,
            "player_name": athlete.get("displayName", ""),
            "team":        item.get("team", {}).get("displayName", ""),
            "status":      item.get("status", ""),
            "detail":      item.get("longComment", item.get("shortComment", "")),
            "fetched_at":  et_naive().isoformat(),
        })
    return injuries


def fetch_news(sport_key: str, limit: int = 10) -> list[dict]:
    mapping = {
        "americanfootball_nfl": ("football",   "nfl"),
        "basketball_nba":       ("basketball", "nba"),
        "baseball_mlb":         ("baseball",   "mlb"),
        "icehockey_nhl":        ("hockey",     "nhl"),
    }
    entry = mapping.get(sport_key)
    if not entry:
        return []
    sport, league = entry
    data = _espn_get(f"{sport}/{league}/news")
    if not data:
        return []
    articles = []
    for item in data.get("articles", [])[:limit]:
        articles.append({
            "headline":    item.get("headline", ""),
            "description": item.get("description", ""),
            "published":   item.get("published", ""),
            "sport":       sport_key,
        })
    return articles


# ── Impact scoring ─────────────────────────────────────────────────────────────

def score_injury_impact(injuries: list[dict], team_name: str) -> float:
    """
    Returns 0-1 impact score.
    0 = major injury to key player on the team we're betting on.
    0.5 = no significant injury.
    1 = opposing team has key injuries (positive).
    """
    if not injuries:
        return 0.5
    team_injuries = [i for i in injuries if team_name.lower() in i.get("team", "").lower()]
    critical = [i for i in team_injuries if i.get("status", "").lower() in INJURY_CRITICAL_STATUSES]
    # Simple heuristic: each critical injury reduces score
    score = max(0.1, 0.5 - len(critical) * 0.1)
    return round(score, 2)


def score_news_impact(articles: list[dict], team_name: str) -> float:
    """Placeholder: returns 0.5 (neutral) until NLP integration is added."""
    return 0.5


# ── Persistence & re-evaluation ────────────────────────────────────────────────

def save_injuries(injuries: list[dict]) -> None:
    from src.db.session import get_db
    from src.db.models import NewsEvent
    with get_db() as db:
        for inj in injuries:
            db.add(NewsEvent(
                sport           = inj["sport"],
                category        = "injury",
                headline        = f"{inj['player_name']} — {inj['status']} ({inj['team']})",
                detail          = inj.get("detail", ""),
                affected_players= [inj["player_name"]],
                affected_teams  = [inj["team"]],
                impact_score    = 0.6 if inj["status"].lower() in INJURY_CRITICAL_STATUSES else 0.3,
            ))


def fetch_all_injuries() -> dict[str, list[dict]]:
    result = {}
    for sport_key in list(SPORTS.values())[:8]:   # cap ESPN calls
        inj = fetch_injuries(sport_key)
        if inj:
            result[sport_key] = inj
    return result


def get_recent_injuries(hours: int = 24) -> list[dict]:
    """Return recent injuries from DB as flat list — used by picks_worker."""
    from src.db.session import get_db
    from src.db.models import NewsEvent
    from datetime import timedelta

    cutoff = et_naive() - timedelta(hours=hours)
    with get_db() as db:
        rows = db.query(NewsEvent).filter(
            NewsEvent.category == "injury",
            NewsEvent.fetched_at >= cutoff,
        ).all()
        result = []
        for row in rows:
            players = row.affected_players or []
            teams = row.affected_teams or []
            headline = row.headline or ""
            # Extract actual status from saved headline "Player — status (team)"
            status = "out"
            for s in INJURY_CRITICAL_STATUSES:
                if s in headline.lower():
                    status = s
                    break
            result.append({
                "sport": row.sport,
                "player_name": players[0] if players else "",
                "team": teams[0] if teams else "",
                "status": status,
                "detail": row.detail or "",
            })
    return result
