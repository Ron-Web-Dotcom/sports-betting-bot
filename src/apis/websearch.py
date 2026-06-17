"""
Web search adapter — Perplexity API.

Used as a "dig deeper" step when AI confidence is borderline (0.72–0.79).
Perplexity searches the web AND reasons over results in one call — no need
to pass raw results back to OpenAI separately.

Only called on games that are already borderline — not every game.
Keeps usage low: 3-6 calls per slip at most.

Sign up: perplexity.ai/api — ~$5/month pay-as-you-go
Model used: llama-3.1-sonar-large-128k-online (web-search enabled)
"""
import logging

logger = logging.getLogger(__name__)

_BASE = "https://api.perplexity.ai"
_MODEL = "sonar"


def _ask(prompt: str) -> str:
    """Single Perplexity call — returns clean text answer with web citations baked in."""
    from src.core.config import PERPLEXITY_API_KEY
    if not PERPLEXITY_API_KEY:
        return ""
    try:
        import httpx
        resp = httpx.post(
            f"{_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model": _MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a sports research assistant. Search the web for the latest "
                            "news on this game/player and return ONLY the most relevant facts: "
                            "injuries, lineup changes, recent form, sharp betting moves, and any "
                            "breaking news from today. Be concise — bullet points, no fluff. "
                            "Cite your sources inline."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 512,
                "temperature": 0.1,   # low temp — we want facts, not creativity
                "return_citations": True,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data    = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        logger.info("Perplexity search returned %d chars", len(content))
        return content
    except Exception as e:
        logger.warning("Perplexity search failed: %s", e)
        return ""


def search_game_news(
    home_team:  str,
    away_team:  str,
    sport_key:  str = "",
    today:      str = "",
) -> str:
    """
    Deep-dive search for a specific matchup.
    Called when AI is 0.72–0.79 and needs more to reach 0.80.
    Returns a ready-to-inject text summary.
    """
    from src.core.timezone import et_naive
    date_str     = today or et_naive().strftime("%B %d, %Y")
    sport_label  = _sport_label(sport_key)

    prompt = (
        f"Search for the latest news on {away_team} vs {home_team} "
        f"({sport_label}, {date_str}). "
        f"I need: injury report, confirmed starting lineup, recent form last 5 games, "
        f"any sharp betting line movement, and anything else that affects who wins today."
    )
    return _ask(prompt)


def search_player_news(player_name: str, stat: str, sport_key: str = "") -> str:
    """
    Deep-dive search for a player prop.
    Called when AI is borderline on a player prop pick.
    """
    from src.core.timezone import et_naive
    date_str    = et_naive().strftime("%B %d, %Y")
    sport_label = _sport_label(sport_key)

    prompt = (
        f"Search for the latest on {player_name} ({sport_label}, {date_str}). "
        f"I need: is they injured or listed? Their last 5 game stats for {stat}. "
        f"Matchup vs today's opponent — is the opponent strong or weak defending this stat? "
        f"Any prop line movement or sharp action on this player today?"
    )
    return _ask(prompt)


def _sport_label(sport_key: str) -> str:
    labels = {
        "basketball_nba":         "NBA",
        "basketball_wnba":        "WNBA",
        "americanfootball_nfl":   "NFL",
        "baseball_mlb":           "MLB",
        "icehockey_nhl":          "NHL",
        "icehockey_pwhl":         "PWHL",
        "mma_mixed_martial_arts": "UFC/MMA",
        "boxing_boxing":          "boxing",
        "golf_pga_tour":          "PGA Tour golf",
        "golf_lpga":              "LPGA golf",
    }
    if "soccer" in sport_key:
        return "soccer"
    if "tennis" in sport_key:
        return "tennis"
    if "rugby" in sport_key:
        return "rugby"
    if "cricket" in sport_key:
        return "cricket"
    return labels.get(sport_key, "sports")
