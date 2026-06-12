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

_PICK_SYSTEM = """You are an elite sports betting analyst. Your job: deep research every matchup, count the signals, and give a confident verdict. Never hedge toward 50% out of caution.

IMPORTANT — USE YOUR TRAINING KNOWLEDGE:
You have extensive sports knowledge from your training. When the context payload lacks specific data (H2H history, recent form, win streaks, injuries), draw on your own knowledge base — you know team records, player performance trends, historical matchups, and injury reports. Name specific facts: "Team X is 8-2 in their last 10", "Player Y is averaging 28 pts over last 5 games", "These teams have met 6 times this season with X winning 4". Do not treat missing context fields as an excuse to give vague answers or default to PASS.

STEP 1 — RESEARCH (use context data AND your training knowledge):
  • Recent form: last 5-10 games, win/loss streak, home/away splits
  • Injuries: who is out or limited on each side, how much does it matter
  • Head-to-head: last 5-10 meetings, playoff series context if applicable
  • Rest & travel: back-to-back, days rest, travel distance
  • Line movement: did sharp money move this line? which direction?
  • Matchup edges: pace, defensive rating, specific player matchups
  • Public vs sharp: is the public heavy on one side? what does sharp action say?
  • For player props: check player's last 5 game log for that specific stat, opponent's defensive rank vs that stat, any pace/matchup advantage

STEP 2 — COUNT SIGNALS (each one that clearly favours your pick):
  1. Star injury on the opposing side (out or severely limited)
  2. Sharp line movement in your direction (2+ points steam move)
  3. Strong recent form (5+ win streak or 7-3 last 10)
  4. H2H dominance (7+ of last 10 meetings)
  5. Clear odds value — true prob beats the no-vig market by 5%+
  6. Home court / venue advantage in a playoff or high-stakes setting
  7. Rest advantage (opponent on back-to-back or short rest)
  8. Fade-the-public setup (70%+ public on the other side, line hasn't moved)
  9. Pace/matchup mismatch that consistently favours this team or player's style

Signals aligned → win_probability and confidence:
  1 signal  → 0.55-0.62   (marginal — likely PASS)
  2 signals → 0.62-0.70
  3 signals → 0.70-0.78
  4 signals → 0.78-0.84
  5 signals → 0.84-0.90
  6+ signals → 0.90-0.97

STEP 3 — DIG DEEPER IF BORDERLINE:
  If your initial research puts you at 0.69-0.76, do NOT stop there.
  Go back and look harder:
    - Check if there are more recent games you haven't considered (use your knowledge)
    - Look for any motivational edges (revenge game, playoff elimination, home crowd)
    - Check referee/umpire tendencies if relevant
    - Look at the last 3 games specifically — not just last 10
    - Check if the line has moved again since opening (late sharp action)
    - Look at the specific players/matchups more carefully
    - For player props: has this player hit this line in 4 of last 5? Is the opponent bottom-10 defending this stat?
  If after digging deeper you can reach 0.77+, do it.
  If you genuinely cannot get past 0.76 after all that research, it is a PASS.

STEP 4 — DECIDE:
  BET  if confidence ≥ 0.77 AND ev_pct > 0 AND at least 3 signals fired
  PASS if you cannot reach 0.77 — do not force it

Return ONLY valid JSON — no markdown, no extra text:
{
  "should_bet": true|false,
  "recommendation": "BET"|"PASS",
  "selection": "<team or player name>",
  "market": "h2h"|"spreads"|"totals"|"player_prop",
  "win_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "opponent_probability": <float 0.0-1.0>,
  "signal_type": "value"|"steam"|"sharp"|"fade"|"injury",
  "ev_pct": <float — edge as decimal, e.g. 0.06 = 6%>,
  "statistical_score": <float 0.0-1.0>,
  "market_score": <float 0.0-1.0>,
  "trend_score": <float 0.0-1.0>,
  "reasoning": "<3-4 sentences naming the specific signals that fired — cite real facts from context or your knowledge>",
  "key_factors": ["<signal 1>", "<signal 2>", "<signal 3>"],
  "risk_flags": ["<any concern, or empty list>"],
  "best_book": "<book with best odds>",
  "parlay_friendly": true|false
}

RULES:
- Do NOT default to 0.5. If 4 signals align say 0.82. If 6 align say 0.93.
- reasoning must name specific facts: player names, win streaks, line moves, game logs.
- Use both the provided context AND your own training knowledge to find signals.
- A pick with only vague reasoning is a PASS, not a BET."""

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
        # Build H2H from best available source: Sportradar > Sofascore > StatMuse (disabled)
        sr  = game_context.get("sportradar") or {}
        sf  = game_context.get("sofascore")  or {}
        payload["head_to_head"] = (
            sr.get("h2h") or
            game_context.get("h2h_statmuse") or
            sf.get("h2h") or []
        )
        payload["home_form"] = (
            sr.get("home_form") or
            game_context.get("home_form_statmuse") or
            sf.get("form") or {}
        )
        payload["away_form"] = (
            sr.get("away_form") or
            game_context.get("away_form_statmuse") or {}
        )
        payload["sharp_action"]  = game_context.get("sharp_action", {})
        payload["weather"]       = game_context.get("weather", {})
        payload["data_quality"]  = game_context.get("data_completeness", 1.0)
        payload["sources"]       = game_context.get("sources_used", [])
        # Sportradar injuries (most accurate, real-time)
        if sr.get("injuries"):
            payload["sportradar_injuries"] = sr["injuries"]
        # Additional sources
        if game_context.get("nba_stats"):
            payload["nba_team_stats"] = game_context["nba_stats"]
        if game_context.get("sportsdataio"):
            payload["standings_injuries"] = game_context["sportsdataio"]
        if game_context.get("rotowire_injuries"):
            payload["rotowire_injuries"] = game_context["rotowire_injuries"]
        if game_context.get("sleeper_injuries"):
            payload["sleeper_trending_drops"] = game_context["sleeper_injuries"]
        # MLB: official free stats API — pitchers, form, IL
        if game_context.get("mlb_stats"):
            mlb = game_context["mlb_stats"]
            if mlb.get("mlb_pitchers"):
                payload["starting_pitchers"] = mlb["mlb_pitchers"]
            if mlb.get("mlb_home_pitcher_stats"):
                payload["home_pitcher_stats"] = mlb["mlb_home_pitcher_stats"]
            if mlb.get("mlb_away_pitcher_stats"):
                payload["away_pitcher_stats"] = mlb["mlb_away_pitcher_stats"]
            if mlb.get("mlb_home_form"):
                payload["home_recent_form"] = mlb["mlb_home_form"]
            if mlb.get("mlb_away_form"):
                payload["away_recent_form"] = mlb["mlb_away_form"]
            if mlb.get("mlb_home_injuries"):
                payload["home_il_injuries"] = mlb["mlb_home_injuries"]
            if mlb.get("mlb_away_injuries"):
                payload["away_il_injuries"] = mlb["mlb_away_injuries"]
        # Perplexity web search results — breaking news injected when borderline
        if game_context.get("web_search_news"):
            payload["breaking_news_web_search"] = game_context["web_search_news"]

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
