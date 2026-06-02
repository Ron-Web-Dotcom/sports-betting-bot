"""Claude AI — analyses individual legs, scores parlays, writes plain-English summaries."""
import json
import logging
import anthropic

from config.settings import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── System prompts ────────────────────────────────────────────────────────────

_LEG_SYSTEM = """You are an elite sports betting analyst. Your job is to evaluate a single betting
leg and return a structured JSON decision.

Input: event details, live odds from multiple books, ESPN injury report, recent news.

Return ONLY valid JSON — no markdown, no commentary:
{
  "should_bet": true | false,
  "selection": "<exact team or player name>",
  "market": "h2h" | "spreads" | "totals" | "player_prop",
  "win_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "signal_type": "value" | "steam" | "fade" | "sharp" | "parlay_leg",
  "reasoning": "<2-3 sentence plain English>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "risk_flags": ["<concern if any>"],
  "best_book": "<book offering best line>",
  "parlay_friendly": true | false
}

Be conservative. Only set should_bet=true when you see genuine edge (3%+ EV).
Set parlay_friendly=true when the leg has 60%+ confidence and fits a 2-4 leg parlay."""

_PARLAY_SYSTEM = """You are an expert parlay analyst. Given a set of pre-screened legs
with positive individual EV, decide whether they combine well into a parlay.

Return ONLY valid JSON:
{
  "approve_parlay": true | false,
  "parlay_type": "standard" | "sgp" | "round_robin",
  "reasoning": "<2-3 sentences>",
  "correlation_risk": "low" | "medium" | "high",
  "recommended_legs": [<list of leg indices to include, 0-based>],
  "risk_flags": ["<any concern>"]
}"""

_SUMMARY_SYSTEM = "You are a sports betting results analyst. Write a short, punchy session summary in plain English using bullet points. Max 6 bullets. Focus on wins, losses, best call, biggest miss."


# ── Public functions ───────────────────────────────────────────────────────────

def analyse_leg(event: dict, injuries: list, news: list, odds_by_book: dict) -> dict | None:
    payload = {
        "event":      event,
        "odds":       odds_by_book,
        "injuries":   injuries[:15],
        "news":       news[:5],
    }
    prompt = f"Evaluate this betting leg:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call(prompt, _LEG_SYSTEM)


def approve_parlay(legs: list[dict]) -> dict | None:
    payload = {"legs": legs}
    prompt = f"Should these legs be combined into a parlay?\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call(prompt, _PARLAY_SYSTEM)


def write_session_summary(bets: list[dict], parlays: list[dict], bankroll: float) -> str:
    payload = {
        "bankroll":     bankroll,
        "straight_bets": bets,
        "parlays":      parlays,
    }
    prompt = f"Write a session summary:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    result = _call(prompt, _SUMMARY_SYSTEM, raw_text=True)
    return result or "Session summary unavailable."


def _call(prompt: str, system: str, raw_text: bool = False) -> dict | str | None:
    try:
        response = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw_text:
            return raw
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Claude JSON parse error: %s | raw=%.100s", exc, raw if 'raw' in dir() else "")
        return None
    except anthropic.APIError as exc:
        logger.error("Claude API error: %s", exc)
        return None
