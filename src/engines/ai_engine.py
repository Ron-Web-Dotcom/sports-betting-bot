"""
Engine 14 — AI Discussion Engine.

Wraps OpenAI for:
1. Single-leg analysis (statistical + market + injury context)
2. Parlay approval
3. Natural language Q&A (/analyze, /player, /team, /explain, /why, /odds)
4. Session summaries
"""
import json
import logging
import httpx
from openai import OpenAI, APIConnectionError, APIError
from src.core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MAX_TOKENS

logger = logging.getLogger(__name__)

# Primary client — direct connection
_client = OpenAI(api_key=OPENAI_API_KEY)

# Fallback proxy client — created on first use
_proxy_client: OpenAI | None = None


def _get_proxy_client() -> OpenAI:
    global _proxy_client
    if _proxy_client is None:
        try:
            from src.core.config import DECODO_PROXY_URL
            from src.apis.base import _next_port
            if DECODO_PROXY_URL:
                port = _next_port()
                proxy_url = f"{DECODO_PROXY_URL}:{port}"
                _proxy_client = OpenAI(
                    api_key=OPENAI_API_KEY,
                    http_client=httpx.Client(proxy=proxy_url, timeout=30.0),
                )
                logger.info("OpenAI proxy fallback client created on port %s", port)
            else:
                _proxy_client = _client  # no proxy configured — reuse direct
        except Exception as e:
            logger.warning("Could not create OpenAI proxy client: %s", e)
            _proxy_client = _client
    return _proxy_client


# ── System prompts ─────────────────────────────────────────────────────────────

_PICK_SYSTEM = """You are an elite sports betting analyst with deep knowledge of statistics, line movement, injuries, and team form. Your job is to find genuine edges and express your true confidence — not hedge everything toward 50%.

Analyse the provided betting opportunity and return ONLY valid JSON — no markdown, no extra text:
{
  "should_bet": true|false,
  "recommendation": "BET"|"PASS",
  "selection": "<team or player name>",
  "market": "moneyline"|"spread"|"total"|"player_prop",
  "win_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "opponent_probability": <float 0.0-1.0>,
  "signal_type": "value"|"steam"|"sharp"|"fade"|"injury",
  "ev_pct": <float — your estimate of edge as decimal>,
  "statistical_score": <float 0.0-1.0>,
  "market_score": <float 0.0-1.0>,
  "trend_score": <float 0.0-1.0>,
  "reasoning": "<3-4 sentences citing specific facts from the context>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "risk_flags": ["<concern or empty list>"],
  "best_book": "<book with best odds>",
  "parlay_friendly": true|false
}

CONFIDENCE GUIDELINES — use ALL available data and express your true conviction:
- 0.50-0.55: Data is thin or contradictory. PASS unless a clear signal exists.
- 0.55-0.65: Moderate edge — one or two clear signals (injury, form, line value).
- 0.65-0.75: Strong edge — multiple signals aligned (sharp money + injury + form).
- 0.75-0.85: Very strong — overwhelming evidence, dominant matchup, key injury.
- 0.85-0.92: Elite conviction — nearly all signals agree, market is mispriced.
- 0.92-1.00: Maximum certainty — every available signal points the same direction,
             the line is significantly off true probability, historical data is definitive.

SIGNAL CHECKLIST — count how many align, then set confidence accordingly:
- Key injury to opposing star player (starter out or severely limited)
- Sharp line movement in our direction (steam move, line dropped 2+ points)
- Team on a 5+ game winning streak or dominant recent form (7-3 last 10)
- Historical H2H dominance (7+ of last 10 head-to-head wins)
- Significant odds value vs true probability (5%+ edge)
- Weather/venue strongly favours one side (home court, wind, altitude)
- Rest advantage (opponent on back-to-back, we are rested)
- Public betting % heavily on the other side (fade the public setup)
- Line opening movement that reversed (sharp reversal signal)

Signals aligned → confidence target:
  2 signals  → 0.60-0.68
  3 signals  → 0.68-0.76
  4 signals  → 0.76-0.84
  5 signals  → 0.84-0.90
  6+ signals → 0.90-0.97

CRITICAL RULES:
- Use EVERY piece of data provided — injuries, sharp action, H2H, form, weather, line movement, all of it.
- Do NOT default to 0.5 or 0.6 out of caution. If the data says 80%, say 80%.
- Do NOT cap yourself. If 6 signals align and the data is definitive, go to 0.93 or higher.
- If the data is genuinely unclear and fewer than 2 signals exist, PASS. Do not force a low-confidence bet.
- The formal day entry posts at 10-11 AM and night entry at 4-5 PM. By those times you have had
  hours of data — deep research should push confidence above 85% for picks that make the entry.
  If your research only gets you to 70%, that is not ready for a formal entry. Keep it at PASS.
- Your confidence score is the foundation the bot uses to build its winning track record.
  Be accurate and bold — not safe and hedged."""

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

    The AI's role here is EXPLANATION only — it receives pre-computed EV and
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

def _chat(messages: list, client: OpenAI | None = None) -> str | None:
    """Single chat call — tries direct, falls back to proxy on connection error."""
    c = client or _client
    try:
        resp = c.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=OPENAI_MAX_TOKENS,
            messages=messages,
        )
        return resp.choices[0].message.content.strip() if resp.choices else None
    except APIConnectionError as e:
        if c is _client:
            logger.warning("OpenAI direct connection failed (%s) — retrying via proxy", e)
            return _chat(messages, client=_get_proxy_client())
        logger.error("OpenAI proxy connection also failed: %s", e)
        return None
    except APIError as e:
        logger.error("OpenAI API error: %s", e)
        return None


def _call_json(prompt: str, system: str) -> dict | None:
    raw = ""
    try:
        raw = _chat([
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]) or ""
        if not raw:
            logger.warning("OpenAI returned empty content")
            return None
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("OpenAI JSON parse error: %s | raw=%r", e, raw[:200])
        return None


def _call_text(prompt: str, system: str) -> str | None:
    return _chat([
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ])
