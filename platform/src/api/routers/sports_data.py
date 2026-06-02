"""
REST endpoints for SportsData.io team and player data.

GET /sports/{sport_key}/teams                     — all teams in a sport
GET /sports/{sport_key}/teams/{team_name}         — single team profile
GET /sports/{sport_key}/teams/{team_name}/roster  — team roster
GET /sports/{sport_key}/players/{player_name}     — player profile search
GET /sports/{sport_key}/standings                 — current standings
GET /sports/{sport_key}/injuries                  — current injury report
GET /sports/{sport_key}/schedule                  — season schedule
"""
import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()


def _sdio_available(sport_key: str) -> None:
    import os
    if not os.getenv("SPORTSDATAIO_KEY", "").strip():
        raise HTTPException(status_code=503, detail="SPORTSDATAIO_KEY not configured")


@router.get("/{sport_key}/teams")
def list_teams(sport_key: str):
    """All teams for a sport."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_all_teams
    return get_all_teams(sport_key)


@router.get("/{sport_key}/teams/{team_name}")
def get_team(sport_key: str, team_name: str):
    """Single team profile by name or abbreviation."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_team_by_name
    team = get_team_by_name(sport_key, team_name)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team '{team_name}' not found")
    return team


@router.get("/{sport_key}/teams/{team_name}/roster")
def get_roster(sport_key: str, team_name: str):
    """Player roster for a team."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_team_roster
    return get_team_roster(sport_key, team_name)


@router.get("/{sport_key}/players")
def search_players(sport_key: str, name: str = Query(..., description="Player name to search")):
    """Search for a player across all team rosters."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_all_teams, get_players_basic
    name_lower = name.lower()
    matches = []
    for team in get_all_teams(sport_key):
        abbrev = team.get("key", "")
        if not abbrev:
            continue
        for p in get_players_basic(sport_key, abbrev):
            if name_lower in p.get("name", "").lower():
                matches.append(p)
    return matches


@router.get("/{sport_key}/standings")
def get_standings(sport_key: str):
    """Current standings."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_standings
    return get_standings(sport_key)


@router.get("/{sport_key}/injuries")
def get_injuries(sport_key: str):
    """Current injury report."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_injuries
    return get_injuries(sport_key)


@router.get("/{sport_key}/schedule")
def get_schedule(sport_key: str):
    """Season schedule."""
    _sdio_available(sport_key)
    from src.apis.sportsdataio import get_schedule
    return get_schedule(sport_key)
