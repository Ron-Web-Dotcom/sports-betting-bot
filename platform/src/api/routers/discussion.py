"""AI Discussion Mode endpoints — maps to the 10 slash commands."""
from fastapi import APIRouter
from pydantic import BaseModel
from src.engines.ai_engine import handle_command

router = APIRouter()

VALID_COMMANDS = {
    "analyze", "player", "team", "parlay",
    "explain", "why", "odds", "results", "bankroll", "top-picks",
}


class DiscussionRequest(BaseModel):
    command: str
    args: str
    context: dict | None = None


@router.post("/")
def discuss(req: DiscussionRequest):
    if req.command not in VALID_COMMANDS:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown command. Valid: {sorted(VALID_COMMANDS)}")
    result = handle_command(req.command, req.args, req.context)
    return {"command": req.command, "response": result}
