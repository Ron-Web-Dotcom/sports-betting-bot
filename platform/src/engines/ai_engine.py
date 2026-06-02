"""
Engine 14 — AI Discussion Engine.

Wraps Claude for:
1. Single-leg analysis (statistical + market + injury context)
2. Parlay approval
3. Natural language Q&A (/analyze, /player, /team, /explain, /why, /odds)
4. Session summaries
"""
import json
import logging
import anthropic
from src.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── System prompts ─────────────────────────────────────────────────────────────

_PICK_SYSTEM = """You are an elite quantitative sports analyst for a commercial sports intelligence platform.

Analyse the provided betting opportunity and return ONLY valid JSON — no markdown, no extra text:
{
  "should_bet": true|false,
  "recommendation": "BET"|"PASS",
  "selection": "<team or player name>",
  "market": "moneyline"|"spread"|"total"|"player_prop",
  "win_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "signal_type": "value"|"steam"|"sharp"|"fade"|"injury",
  "ev_pct": <float — your estimate of edge as decimal>,
  "statistical_score": <float 0.0-1.0>,
  "market_score": <float 0.0-1.0>,
  "trend_score": <float 0.0-1.0>,
  "reasoning": "<3-4 sentences plain English>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "risk_flags": ["<concern or empty list>"],
  "best_book": "<book with best odds>",
  "parlay_friendly": true|false
}

Be conservative. Only recommend BET when edge is genuine and statistically supported."""

_DISCUSSION_SYSTEM = """You are the AI analyst for a Sports Intelligence Platform.
Answer questions about EV, risk, confidence, injuries, matchups, market movement, CLV, and statistical reasoning.
Be concise, data-driven, and actionable. Reference numbers when available.
Format responses for Discord: use **bold** for key numbers and findings."""

_SUMMARY_SYSTEM = """Write a concise daily performance summary for a sports betting platform.
Use bullet points. Include: record (W-L), units P&L, best pick, worst pick, CLV, notable insights.
Keep it under 300 words. Format for Discord."""


# ── Core analysis ──────────────────────────────────────────────────────────────

def analyse_pick(
    event:        dict,
    injuries:     list,
    news:         list,
    odds_by_book: dict,
    game_context: dict | None = None,
) -> dict | None:
    """
    Analyse a betting opportunity using all available real-world data.

    game_context is a rich dict from data_hub.build_game_context() containing
    H2H history, recent form, sharp action, weather, and injury reports from
    ESPN, StatMuse, RotoWire, and Sleeper. The more context provided, the
    stronger and more traceable the reasoning will be.

    Claude's role here is EXPLANATION only — it receives pre-computed EV and
    confidence scores and must justify them with specific, verifiable factors.
    It does not override the EV model.
    """
    payload = {
        "event":        event,
        "injuries":     injuries[:15],
        "news":         news[:5],
        "odds":         odds_by_book,
    }

    if game_context:
        # Include only the most signal-rich context keys to stay within token budget
        payload["head_to_head"]   = game_context.get("h2h_statmuse", {})
        payload["home_form"]      = game_context.get("home_form_statmuse", {})
        payload["away_form"]      = game_context.get("away_form_statmuse", {})
        payload["sharp_action"]   = game_context.get("sharp_action", {})
        payload["weather"]        = game_context.get("weather", {})
        payload["data_quality"]   = game_context.get("data_completeness", 1.0)
        payload["sources"]        = game_context.get("sources_used", [])

    prompt = f"Analyse this betting opportunity:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call_json(prompt, _PICK_SYSTEM)


def approve_parlay(legs: list[dict]) -> dict | None:
    system = """Evaluate whether these legs should be combined into a parlay.
Return ONLY valid JSON:
{
  "approve": true|false,
  "parlay_type": "standard"|"sgp"|"round_robin",
  "correlation_risk": "low"|"medium"|"high",
  "adjusted_ev_pct": <float>,
  "reasoning": "<2-3 sentences>",
  "risk_flags": []
}"""
    prompt = f"Evaluate this parlay:\n\n```json\n{json.dumps(legs, indent=2, default=str)}\n```"
    return _call_json(prompt, system)


# ── Discord command handlers ───────────────────────────────────────────────────

def handle_command(command: str, args: str, context: dict | None = None) -> str:
    """
    Handle Discord slash commands.
    command: analyze|player|team|parlay|explain|why|odds|results|bankroll|top-picks
    """
    prompts = {
        "analyze":   f"Analyse this betting situation: {args}",
        "player":    f"Provide a betting analysis for this player: {args}. Cover recent form, props value, injury status.",
        "team":      f"Analyse this team for betting purposes: {args}. Cover recent form, trends, injuries, matchups.",
        "parlay":    f"Evaluate this parlay for betting: {args}. Cover correlation, combined EV, risk.",
        "explain":   f"Explain this betting concept in simple terms: {args}",
        "why":       f"Explain why this bet was recommended or why it won/lost: {args}",
        "odds":      f"Explain what these odds mean and how to calculate EV: {args}",
        "results":   f"Summarise these betting results: {args}",
        "bankroll":  f"Give bankroll management advice for this situation: {args}",
        "top-picks": f"Summarise the top picks available right now based on: {args}",
    }
    prompt = prompts.get(command, f"Answer this sports betting question: {args}")
    if context:
        prompt += f"\n\nContext: {json.dumps(context, default=str)}"
    return _call_text(prompt, _DISCUSSION_SYSTEM) or "Analysis unavailable at this time."


def write_daily_summary(
    picks: list[dict],
    results: list[dict],
    clv_stats: dict,
    bankroll: float,
) -> str:
    payload = {
        "picks":    picks[:20],
        "results":  results[:20],
        "clv":      clv_stats,
        "bankroll": bankroll,
    }
    prompt = f"Write a daily summary:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call_text(prompt, _SUMMARY_SYSTEM) or "Summary unavailable."


def write_weekly_summary(stats: dict) -> str:
    prompt = f"Write a weekly performance summary:\n\n```json\n{json.dumps(stats, indent=2, default=str)}\n```"
    return _call_text(prompt, _SUMMARY_SYSTEM) or "Summary unavailable."


# ── Internal helpers ───────────────────────────────────────────────────────────

def _call_json(prompt: str, system: str) -> dict | None:
    raw = ""
    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL, max_tokens=CLAUDE_MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip() if resp.content else ""
        if not raw:
            logger.warning("Claude returned empty content")
            return None
        # Strip markdown code fences Claude sometimes wraps JSON in
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Claude JSON parse error: %s | raw=%r", e, raw[:200])
        return None
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None


def _call_text(prompt: str, system: str) -> str | None:
    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL, max_tokens=CLAUDE_MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip() if resp.content else None
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        return None
