"""Claude AI decision engine — analyses opportunities and returns structured signals."""
import json
import logging
from typing import Any

import anthropic

from config.settings import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert sports betting analyst and quantitative trader.
Your job is to analyse betting opportunities and return a structured JSON decision.

You will receive:
- Betting event details (teams, sport, market, odds)
- Live injury reports from ESPN
- Recent news and player trends
- Lines from multiple platforms (PrizePicks, Underdog, DraftKings, FanDuel)

You must return ONLY valid JSON in this exact schema — no markdown, no extra text:
{
  "should_bet": true | false,
  "selection": "<team or player name to bet on>",
  "market": "<h2h | spread | total | player_prop>",
  "win_probability": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentence plain-English explanation>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "risk_flags": ["<any concern or reason to avoid>"],
  "recommended_platform": "<prizepicks | underdog | draftkings | fanduel | the_odds_api>"
}

Be conservative. Only recommend a bet when you see genuine edge. When uncertain, set should_bet to false."""


def analyse_opportunity(event: dict, injuries: list[dict], news: list[dict], lines: dict) -> dict | None:
    """
    Ask Claude to evaluate a betting opportunity.
    Returns parsed JSON signal or None on failure.
    """
    payload = {
        "event": event,
        "injuries": injuries[:20],
        "recent_news": news[:5],
        "platform_lines": lines,
    }

    prompt = (
        f"Analyse this betting opportunity and return a JSON decision:\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        signal = json.loads(raw)
        signal["raw_response"] = raw
        return signal
    except json.JSONDecodeError as exc:
        logger.error("Claude returned non-JSON: %s — %s", exc, raw[:200])
        return None
    except anthropic.APIError as exc:
        logger.error("Claude API error: %s", exc)
        return None


def batch_analyse(opportunities: list[dict]) -> list[dict]:
    """Run analysis on a list of opportunities; skip failed analyses."""
    signals = []
    for opp in opportunities:
        signal = analyse_opportunity(
            event=opp.get("event", {}),
            injuries=opp.get("injuries", []),
            news=opp.get("news", []),
            lines=opp.get("lines", {}),
        )
        if signal:
            signal["_source"] = opp
            signals.append(signal)
    return signals


def summarise_session(bets: list[dict], bankroll: float) -> str:
    """Ask Claude to write a plain-English session summary for Discord."""
    if not bets:
        return "No bets placed this session."

    prompt = (
        f"Write a short, plain-English session summary (max 5 bullet points) for these bets. "
        f"Current bankroll: ${bankroll:.2f}.\n\n"
        f"```json\n{json.dumps(bets, indent=2, default=str)}\n```"
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as exc:
        logger.error("Claude session summary error: %s", exc)
        return "Session summary unavailable."
