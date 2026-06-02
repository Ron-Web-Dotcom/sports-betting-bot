"""AI Discussion Mode endpoints."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from src.engines.ai_engine import handle_command

router = APIRouter()

VALID_COMMANDS = {
    "analyze", "player", "team", "parlay",
    "explain", "why", "odds", "results", "bankroll", "top-picks",
}


class DiscussionRequest(BaseModel):
    command: str
    args: str = Field(max_length=2000)
    context: dict | None = None


@router.post("/")
def discuss(req: DiscussionRequest):
    if req.command not in VALID_COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unknown command. Valid: {sorted(VALID_COMMANDS)}")
    result = handle_command(req.command, req.args, req.context)
    if result is None:
        raise HTTPException(status_code=503, detail="Analysis service temporarily unavailable")
    return {"command": req.command, "response": result}
