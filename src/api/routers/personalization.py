"""Personalization endpoints — user profiles and settings."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

_MAX_BANKROLL = 10_000_000.0
_VALID_SPORTS: set | None = None  # lazy-loaded


def _valid_sports() -> set:
    global _VALID_SPORTS
    if _VALID_SPORTS is None:
        from src.core.config import SPORTS
        _VALID_SPORTS = set(SPORTS.keys()) | set(SPORTS.values())
    return _VALID_SPORTS


class BankrollUpdate(BaseModel):
    bankroll: float = Field(gt=0, le=_MAX_BANKROLL)


class SportsUpdate(BaseModel):
    sports: list[str] = Field(max_length=20)


class RiskUpdate(BaseModel):
    risk_profile: str


@router.get("/{user_id}")
def get_user_profile(user_id: str):
    from src.engines.personalization_engine import get_profile
    from src.db.session import get_db
    from src.db.models import UserProfile as UserProfileModel

    # Return 404 for users that genuinely don't exist in the DB
    with get_db() as db:
        exists = db.query(UserProfileModel).filter(
            UserProfileModel.user_id == user_id
        ).first() is not None

    p = get_profile(user_id)
    return {
        "user_id":      p.user_id,
        "exists":       exists,
        "bankroll":     p.bankroll,
        "risk_profile": p.risk_profile,
        "sports":       p.sports,
        "max_units":    p.max_units,
        "min_ev":       p.min_ev,
        "created_at":   p.created_at.isoformat() if p.created_at else None,
        "updated_at":   p.updated_at.isoformat() if p.updated_at else None,
    }


@router.post("/{user_id}/bankroll")
def set_bankroll(user_id: str, body: BankrollUpdate):
    from src.engines.personalization_engine import get_profile, save_profile
    p = get_profile(user_id)
    p.bankroll = body.bankroll
    save_profile(p)
    return {"user_id": user_id, "bankroll": p.bankroll}


@router.post("/{user_id}/sports")
def set_sports(user_id: str, body: SportsUpdate):
    from src.engines.personalization_engine import get_profile, save_profile
    unknown = [s for s in body.sports if s not in _valid_sports()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown sports: {unknown}")
    p = get_profile(user_id)
    p.sports = body.sports
    save_profile(p)
    return {"user_id": user_id, "sports": p.sports}


@router.post("/{user_id}/risk")
def set_risk(user_id: str, body: RiskUpdate):
    from src.engines.personalization_engine import get_profile, save_profile, RISK_PROFILES
    if body.risk_profile not in RISK_PROFILES:
        raise HTTPException(status_code=400, detail=f"Invalid risk_profile. Choose: {list(RISK_PROFILES)}")
    p = get_profile(user_id)
    p.risk_profile = body.risk_profile
    p.max_units = RISK_PROFILES[body.risk_profile]["max_units"]
    p.min_ev = RISK_PROFILES[body.risk_profile]["min_ev"]
    save_profile(p)
    return {"user_id": user_id, "risk_profile": p.risk_profile}
