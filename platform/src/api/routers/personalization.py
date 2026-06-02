"""Personalization endpoints — user profiles and settings."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class BankrollUpdate(BaseModel):
    bankroll: float


class SportsUpdate(BaseModel):
    sports: list[str]


class RiskUpdate(BaseModel):
    risk_profile: str


@router.get("/{user_id}")
def get_user_profile(user_id: str):
    from src.engines.personalization_engine import get_profile
    p = get_profile(user_id)
    return {
        "user_id": p.user_id,
        "bankroll": p.bankroll,
        "risk_profile": p.risk_profile,
        "sports": p.sports,
        "max_units": p.max_units,
        "min_ev": p.min_ev,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
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
    p = get_profile(user_id)
    p.sports = body.sports
    save_profile(p)
    return {"user_id": user_id, "sports": p.sports}


@router.post("/{user_id}/risk")
def set_risk(user_id: str, body: RiskUpdate):
    from src.engines.personalization_engine import get_profile, save_profile, RISK_PROFILES
    if body.risk_profile not in RISK_PROFILES:
        raise HTTPException(status_code=400, detail="Invalid risk_profile")
    p = get_profile(user_id)
    p.risk_profile = body.risk_profile
    p.max_units = RISK_PROFILES[body.risk_profile]["max_units"]
    p.min_ev = RISK_PROFILES[body.risk_profile]["min_ev"]
    save_profile(p)
    return {"user_id": user_id, "risk_profile": p.risk_profile}
