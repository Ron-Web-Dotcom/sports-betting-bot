"""
Polymarket adapter.

Polymarket is a decentralised prediction market on Polygon/USDC.
Sports markets include: NFL, NBA, MLB, NHL, Soccer, UFC, Tennis, Golf,
Olympics, World Cup, and more.

Public API — no key required. Requests routed through Decodo proxy.
Base: https://gamma-api.polymarket.com

Key endpoint:
  GET /markets?active=true&limit=100   — all open markets
  GET /events?active=true&limit=100    — grouped events (better for sports)

Prices: 0.0–1.0 implied probability (e.g. 0.65 = 65% chance YES wins)
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://gamma-api.polymarket.com"

# Sports keywords to filter markets — Polymarket has no sport category flag
_SPORT_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl", "ufc", "mma",
    "champions league", "premier league", "mls",
    "wimbledon", "us open", "french open", "australian open",
    "masters", "pga", "formula 1", "f1",
    "defeat", "beat",
]

# Patterns that indicate a tournament FUTURES contract — NOT a single-game pick
_FUTURES_PATTERNS = [
    "win the", "win the 2", "world cup", "fifa", "championship", "champion",
    "stanley cup", "super bowl", "world series", "nba finals",
    "advance to", "qualify for", "make the playoffs", "make playoffs",
    "win series", "win title", "presidential", "election", "president",
    "primary", "governor", "senate", "congress", "bitcoin", "crypto",
    "will it", "by end of", "before ", "next year", "in 202",
]

_SPORT_MAP = {
    "nba":      "basketball_nba",
    "nfl":      "americanfootball_nfl",
    "mlb":      "baseball_mlb",
    "nhl":      "icehockey_nhl",
    "ufc":      "mma_mixed_martial_arts",
    "mma":      "mma_mixed_martial_arts",
    "world cup": "soccer_fifa_world_cup",
    "fifa":     "soccer_fifa_world_cup",
    "premier league": "soccer_epl",
    "champions league": "soccer_uefa_champs_league",
    "mls":      "soccer_usa_mls",
    "wimbledon": "tennis_atp_french_open",
    "us open":  "tennis_atp_french_open",
    "french open": "tennis_atp_french_open",
    "masters":  "golf_masters_tournament_winner",
    "pga":      "golf_masters_tournament_winner",
    "formula 1": "motorsport_formula_1",
    "f1":       "motorsport_formula_1",
    "stanley cup": "icehockey_nhl",
    "super bowl": "americanfootball_nfl",
    "world series": "baseball_mlb",
    "nba finals": "basketball_nba",
}


def _detect_sport(question: str) -> str:
    q = question.lower()
    for keyword, sport_key in _SPORT_MAP.items():
        if keyword in q:
            return sport_key
    return "sports"


def _is_futures_market(question: str) -> bool:
    """Return True if this looks like a tournament futures/politics contract."""
    q = question.lower()
    return any(pat in q for pat in _FUTURES_PATTERNS)


def _is_game_day_market(end_date: str) -> bool:
    """Return True only if the market ends within 48 hours (i.e. a single game)."""
    if not end_date:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        # Polymarket end_date formats: ISO string or Unix timestamp
        if isinstance(end_date, (int, float)):
            dt = datetime.fromtimestamp(float(end_date), tz=timezone.utc)
        else:
            # Try ISO parse — strip trailing Z
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return timedelta(0) <= (dt - now) <= timedelta(hours=48)
    except Exception:
        return False


def _is_sports_market(question: str) -> bool:
    q = question.lower()
    if _is_futures_market(question):
        return False
    return any(kw in q for kw in _SPORT_KEYWORDS)


def _parse_market(m: dict) -> dict | None:
    question = m.get("question") or m.get("title") or ""
    if not question:
        return None
    end_date = m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso") or ""
    # Only accept single-game markets (ends within 48 h) that are not futures/politics
    if not _is_game_day_market(end_date):
        return None
    if not _is_sports_market(question):
        return None

    # Prices — Polymarket returns array matching outcomes order
    outcomes_raw = m.get("outcomes", [])
    prices_raw   = m.get("outcomePrices", [])

    # Normalise outcomes to list of strings
    if isinstance(outcomes_raw, str):
        import json as _json
        try:
            outcomes_raw = _json.loads(outcomes_raw)
        except Exception:
            outcomes_raw = []
    if isinstance(prices_raw, str):
        import json as _json
        try:
            prices_raw = _json.loads(prices_raw)
        except Exception:
            prices_raw = []

    outcomes: list[str] = [str(o) for o in (outcomes_raw or [])]
    prices: list[float] = []
    for p in (prices_raw or []):
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(0.0)

    # Build yes/no prices — for binary markets (Yes/No)
    # For multi-outcome, use the two highest-probability outcomes
    yes_price = no_price = None
    if len(outcomes) == 2:
        if outcomes[0].lower() in ("yes", "true"):
            yes_price = prices[0] if prices else None
            no_price  = prices[1] if len(prices) > 1 else None
        else:
            # Team A vs Team B — treat first as "yes"
            yes_price = prices[0] if prices else None
            no_price  = prices[1] if len(prices) > 1 else None
    elif len(outcomes) >= 2 and prices:
        # Multi-outcome — surface the top two by probability
        ranked = sorted(zip(outcomes, prices), key=lambda x: x[1], reverse=True)
        yes_price = ranked[0][1]
        no_price  = ranked[1][1] if len(ranked) > 1 else None

    volume   = m.get("volume") or m.get("volume24hr") or m.get("volumeNum", 0)
    end_date = m.get("endDate") or m.get("end_date_iso") or ""

    return {
        "market_id":  m.get("id") or m.get("conditionId") or "",
        "title":      question,
        "outcomes":   outcomes,
        "yes_price":  round(float(yes_price), 4) if yes_price is not None else None,
        "no_price":   round(float(no_price),  4) if no_price  is not None else None,
        "volume":     float(volume) if volume else 0.0,
        "end_date":   end_date,
        "sport_key":  _detect_sport(question),
        "source":     "polymarket",
    }


def get_sports_markets(limit: int = 200) -> list[dict]:
    """
    Fetch active sports prediction markets from Polymarket.
    Routed through Decodo proxy automatically (not in bypass list).
    """
    data = get_json(
        f"{_BASE}/markets",
        params={"active": "true", "closed": "false", "limit": limit},
    )

    if not data:
        logger.warning("Polymarket: no data returned")
        return []

    raw = data if isinstance(data, list) else data.get("markets", data.get("data", []))
    if not isinstance(raw, list):
        logger.warning("Polymarket: unexpected response shape: %s", type(raw))
        return []

    out = []
    for m in raw:
        parsed = _parse_market(m)
        if parsed:
            out.append(parsed)

    # Sort by volume descending — higher volume = more liquid = more reliable price
    out.sort(key=lambda x: x.get("volume", 0), reverse=True)
    logger.info("Polymarket: %d sports markets fetched", len(out))
    return out
