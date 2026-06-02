"""User feedback engine — collect pick ratings and derive quality signals."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class PickFeedback:
    pick_id: int
    user_id: str
    rating: Literal["helpful", "not_helpful"]
    explanation_rating: int  # 1-5
    comment: str = ""
    submitted_at: datetime = field(default_factory=datetime.utcnow)


def submit_feedback(feedback: PickFeedback) -> None:
    from src.db.session import get_db
    from src.db.models import UserFeedback

    with get_db() as db:
        row = UserFeedback(
            pick_id=feedback.pick_id,
            user_id=feedback.user_id,
            rating=feedback.rating,
            explanation_rating=max(1, min(5, feedback.explanation_rating)),
            comment=feedback.comment,
            submitted_at=feedback.submitted_at,
        )
        db.add(row)


def get_pick_quality_score(pick_id: int) -> float:
    from src.db.session import get_db
    from src.db.models import UserFeedback

    with get_db() as db:
        rows = db.query(UserFeedback).filter(UserFeedback.pick_id == pick_id).all()

    if not rows:
        return 0.0
    scores = [1.0 if r.rating == "helpful" else 0.0 for r in rows]
    return sum(scores) / len(scores)


def get_explanation_quality(pick_id: int) -> float:
    from src.db.session import get_db
    from src.db.models import UserFeedback

    with get_db() as db:
        rows = db.query(UserFeedback).filter(UserFeedback.pick_id == pick_id).all()

    if not rows:
        return 0.0
    return sum(r.explanation_rating for r in rows) / len(rows)


def get_overall_satisfaction(period: str = "weekly") -> dict:
    from src.db.session import get_db
    from src.db.models import UserFeedback
    from datetime import timedelta

    cutoffs = {
        "daily":   timedelta(days=1),
        "weekly":  timedelta(weeks=1),
        "monthly": timedelta(days=30),
    }
    delta = cutoffs.get(period, timedelta(weeks=1))
    since = datetime.utcnow() - delta

    with get_db() as db:
        rows = db.query(UserFeedback).filter(UserFeedback.submitted_at >= since).all()

    total = len(rows)
    if total == 0:
        return {"satisfaction_rate": 0.0, "total_ratings": 0, "avg_explanation": 0.0}

    helpful = sum(1 for r in rows if r.rating == "helpful")
    avg_explanation = sum(r.explanation_rating for r in rows) / total
    return {
        "satisfaction_rate": helpful / total,
        "total_ratings": total,
        "avg_explanation": avg_explanation,
    }


def apply_feedback_to_model(picks: list[dict]) -> list[dict]:
    # boost picks whose sport/market combo has historically high satisfaction
    from src.db.session import get_db
    from src.db.models import UserFeedback, Pick as PickModel

    with get_db() as db:
        feedback_rows = db.query(UserFeedback).all()
        # build sport+market -> avg rating map
        combo_scores: dict[str, list[float]] = {}
        for fb in feedback_rows:
            pick_row = db.query(PickModel).filter(PickModel.id == fb.pick_id).first()
            if pick_row:
                key = f"{pick_row.sport}:{pick_row.market}"
                combo_scores.setdefault(key, [])
                combo_scores[key].append(1.0 if fb.rating == "helpful" else 0.0)

    avg_scores = {k: sum(v) / len(v) for k, v in combo_scores.items()}

    result = []
    for pick in picks:
        key = f"{pick.get('sport', '')}:{pick.get('market', '')}"
        score = avg_scores.get(key, 0.5)
        boosted = dict(pick)
        # boost ev_pct by up to 20% if score > 0.7
        if score > 0.7:
            boosted["ev_pct"] = pick.get("ev_pct", 0) * (1 + (score - 0.7) * 0.5)
        result.append(boosted)
    return result
