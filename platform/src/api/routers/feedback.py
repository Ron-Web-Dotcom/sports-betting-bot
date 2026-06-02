"""Feedback endpoints — submit ratings and query quality scores."""
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()


class FeedbackSubmit(BaseModel):
    pick_id: int
    user_id: str
    rating: str  # helpful|not_helpful
    explanation_rating: int = 3
    comment: str = ""


@router.post("/")
def submit(body: FeedbackSubmit):
    from src.engines.feedback_engine import submit_feedback, PickFeedback
    fb = PickFeedback(
        pick_id=body.pick_id,
        user_id=body.user_id,
        rating=body.rating,
        explanation_rating=body.explanation_rating,
        comment=body.comment,
        submitted_at=datetime.utcnow(),
    )
    submit_feedback(fb)
    return {"status": "ok", "pick_id": body.pick_id}


@router.get("/{pick_id}/quality")
def pick_quality(pick_id: int):
    from src.engines.feedback_engine import get_pick_quality_score, get_explanation_quality
    return {
        "pick_id": pick_id,
        "quality_score": get_pick_quality_score(pick_id),
        "explanation_quality": get_explanation_quality(pick_id),
    }
