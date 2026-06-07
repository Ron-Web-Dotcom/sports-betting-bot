"""Feedback endpoints — submit ratings and query quality scores."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime

router = APIRouter()


class FeedbackSubmit(BaseModel):
    pick_id: int = Field(gt=0)
    user_id: str = Field(min_length=1, max_length=100)
    rating: str = Field(pattern=r"^(helpful|not_helpful)$")
    explanation_rating: int = Field(default=3, ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


@router.post("/")
def submit(body: FeedbackSubmit):
    from src.engines.feedback_engine import submit_feedback, PickFeedback
    from src.db.session import get_db
    from src.db.models import Pick

    # Verify pick exists before accepting feedback
    with get_db() as db:
        if not db.query(Pick).filter(Pick.id == body.pick_id).first():
            raise HTTPException(status_code=404, detail="Pick not found")

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
    from src.db.session import get_db
    from src.db.models import Pick

    with get_db() as db:
        if not db.query(Pick).filter(Pick.id == pick_id).first():
            raise HTTPException(status_code=404, detail="Pick not found")

    return {
        "pick_id":            pick_id,
        "quality_score":      get_pick_quality_score(pick_id),
        "explanation_quality": get_explanation_quality(pick_id),
    }
