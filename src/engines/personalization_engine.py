"""User personalization engine — profiles, risk settings, pick filtering."""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from src.core.timezone import et_naive

RISK_PROFILES = {
    "conservative": {"max_units": 2, "min_ev": 0.03},
    "balanced":     {"max_units": 3, "min_ev": 0.02},
    "aggressive":   {"max_units": 5, "min_ev": 0.01},
}

RiskProfile = Literal["conservative", "balanced", "aggressive"]


@dataclass
class UserProfile:
    user_id: str
    bankroll: float = 1000.0
    risk_profile: RiskProfile = "balanced"
    sports: list[str] = field(default_factory=list)
    max_units: int = 3
    min_ev: float = 0.02
    created_at: datetime = field(default_factory=et_naive)
    updated_at: datetime = field(default_factory=et_naive)


def _parse_sports(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def get_profile(user_id: str) -> UserProfile:
    from src.db.session import get_db
    from src.db.models import UserProfile as UserProfileModel

    with get_db() as db:
        row = db.query(UserProfileModel).filter(UserProfileModel.user_id == user_id).first()
        if row is None:
            return UserProfile(user_id=user_id)
        return UserProfile(
            user_id=row.user_id,
            bankroll=row.bankroll,
            risk_profile=row.risk_profile,
            sports=_parse_sports(row.sports),
            max_units=row.max_units,
            min_ev=row.min_ev,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def save_profile(profile: UserProfile) -> None:
    from src.db.session import get_db
    from src.db.models import UserProfile as UserProfileModel

    with get_db() as db:
        row = db.query(UserProfileModel).filter(UserProfileModel.user_id == profile.user_id).first()
        if row is None:
            row = UserProfileModel(user_id=profile.user_id)
            db.add(row)
        row.bankroll = profile.bankroll
        row.risk_profile = profile.risk_profile
        row.sports = profile.sports
        row.max_units = profile.max_units
        row.min_ev = profile.min_ev
        row.updated_at = et_naive()


def apply_profile(picks: list[dict], profile: UserProfile) -> list[dict]:
    result = []
    for pick in picks:
        # filter by sport
        if profile.sports and pick.get("sport") not in profile.sports:
            continue
        # filter by min_ev
        ev = pick.get("ev_pct", 0)
        if ev < profile.min_ev:
            continue
        # cap units
        filtered = dict(pick)
        if filtered.get("units", 0) > profile.max_units:
            filtered["units"] = profile.max_units
        result.append(filtered)
    return result
