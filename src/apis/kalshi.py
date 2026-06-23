"""
Kalshi adapter.

Kalshi is a regulated prediction/event contracts market covering sports,
politics, economics, and more. Sports markets include NFL, NBA, MLB, NHL,
NCAAB, NCAAF, Tennis, Golf, Soccer, UFC, and F1.

Official public API with full documentation at docs.kalshi.com.
Base: https://external-api.kalshi.com/trade-api/v2

Auth: RSA-PSS SHA256 signature
  Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
  Sign: timestamp(ms) + METHOD + path  with your RSA private key

Set env vars:
  KALSHI_API_KEY_ID     — your API key ID from dashboard
  KALSHI_PRIVATE_KEY    — RSA private key (PEM string or path to .pem file)
"""
import base64
import hashlib
import logging
import os
import time
from datetime import datetime

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Sports-relevant category tags Kalshi uses
_SPORT_TAGS = {
    "americanfootball_nfl":           ["NFL", "FOOTBALL"],
    "basketball_nba":                 ["NBA", "BASKETBALL"],
    "baseball_mlb":                   ["MLB", "BASEBALL"],
    "icehockey_nhl":                  ["NHL", "HOCKEY"],
    "basketball_ncaab":               ["NCAAB", "COLLEGE_BASKETBALL"],
    "americanfootball_ncaaf":         ["NCAAF", "COLLEGE_FOOTBALL"],
    "soccer_epl":                     ["SOCCER", "EPL", "FOOTBALL"],
    "soccer_usa_mls":                 ["MLS", "SOCCER"],
    "soccer_fifa_world_cup":          ["WORLD CUP", "FIFA", "SOCCER", "FOOTBALL"],
    "tennis_atp_french_open":         ["TENNIS"],
    "mma_mixed_martial_arts":         ["UFC", "MMA"],
    "golf_us_open_winner":            ["GOLF", "PGA", "US OPEN"],
    "motorsport_formula_1":           ["F1", "FORMULA1"],
}


def _key_id() -> str:
    return os.getenv("KALSHI_API_KEY_ID", "").strip()


def _private_key():
    """Load RSA private key from env var (PEM string) or file path."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return None

    raw = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return None

    # May be a file path or inline PEM
    if os.path.isfile(raw):
        with open(raw, "rb") as f:
            pem = f.read()
    else:
        pem = raw.encode()

    try:
        return load_pem_private_key(pem, password=None)
    except Exception as e:
        logger.warning("Kalshi: could not load private key: %s", e)
        return None


def _sign_request(method: str, path: str) -> dict | None:
    """Build Kalshi auth headers for a request."""
    key_id = _key_id()
    private_key = _private_key()
    if not key_id or not private_key:
        return None

    try:
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes

        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = private_key.sign(msg, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY":       key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
    except Exception as e:
        logger.warning("Kalshi: request signing failed: %s", e)
        return None


def _get(path: str, params: dict | None = None) -> dict | list | None:
    headers = _sign_request("GET", path) or {}
    try:
        return get_json(f"{_BASE}{path}", params=params, headers=headers)
    except Exception as e:
        logger.warning("Kalshi GET %s failed: %s", path, e)
        return None


def get_markets(sport_key: str | None = None, limit: int = 200) -> list[dict]:
    """
    Fetch active event markets from Kalshi.
    Optionally filtered by sport_key — returns all sports markets if None.
    """
    params: dict = {"limit": limit, "status": "open"}

    data = _get("/markets", params)
    if not data:
        return []

    markets_raw = data.get("markets", []) if isinstance(data, dict) else []

    # Filter by sport tags if requested
    target_tags = set()
    if sport_key:
        for tag in _SPORT_TAGS.get(sport_key, []):
            target_tags.add(tag.upper())

    out = []
    for m in markets_raw:
        tags = [t.upper() for t in (m.get("tags") or [])]
        category = (m.get("category") or "").upper()

        if target_tags and not (target_tags & set(tags)) and category not in target_tags:
            continue

        yes_bid  = m.get("yes_bid",  0) / 100  # Kalshi prices are in cents (0-100)
        yes_ask  = m.get("yes_ask",  0) / 100
        no_bid   = m.get("no_bid",   0) / 100
        no_ask   = m.get("no_ask",   0) / 100

        # Convert to American odds for consistency
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid

        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "category":     category,
            "tags":         tags,
            "yes_price":    round(yes_mid, 4),    # implied prob of YES (0-1)
            "no_price":     round(no_mid,  4),    # implied prob of NO  (0-1)
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "result":       m.get("result", ""),
            "sport_key":    sport_key or "",
            "source":       "kalshi",
        })

    return out


_SPORTS_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl", "ufc", "mma", "wnba",
    "champions league", "premier league", "mls", "liga", "serie a",
    "wimbledon", "us open", "french open", "masters", "pga",
    "formula 1", "f1", "ncaa", "fifa", "world cup", "copa",
    # single-game terms
    "game ", "match", "points", "score", "innings", "quarter",
    "goals", "goal", "runs", "sets", "aces",
    # US teams
    "heat", "celtics", "lakers", "warriors", "knicks", "nuggets",
    "yankees", "dodgers", "mets", "red sox", "cubs", "astros",
    "orioles", "baltimore", "san diego", "kansas city", "detroit",
    "tampa bay", "atlanta", "los angeles",
    "oilers", "panthers", "rangers", "avalanche", "lightning",
    # World soccer teams/tournaments
    "england", "portugal", "brazil", "france", "germany", "spain",
    "argentina", "morocco", "algeria", "colombia", "croatia",
    "both teams to score", "btts",
    "basketball", "baseball", "hockey", "soccer", "football",
    "tennis", "golf", "racing",
    "total ", "over ", "under ", "wins by",
]

# Futures / politics patterns — always block (only long-term non-game markets)
_KALSHI_FUTURES = [
    "presidential", "election", "president",
    "primary", "governor", "senate", "congress", "bitcoin", "crypto",
    "before 20", "next year", "erupt before", "land on mars",
    "will humans colonize", "visit mars",
    "make the playoffs", "make playoffs", "win the championship",
    "win the title", "win the league", "win the serie",
]


def _kalshi_is_game_day(close_time: str) -> bool:
    """Return True if market closes within 36 hours (or has no close_time — assume live)."""
    if not close_time:
        return True  # no close_time = treat as current/live
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return timedelta(-1) <= (dt - now) <= timedelta(hours=36)
    except Exception:
        return True  # unparseable = include rather than drop


def get_sports_markets() -> list[dict]:
    """
    Fetch active SINGLE-GAME sports markets from Kalshi (closes within 36 h).
    Excludes tournament futures and politics.
    """
    markets_raw: list[dict] = []
    for params in [
        {"limit": 200, "status": "open", "category": "Sports"},
        {"limit": 200, "status": "open"},
        {"limit": 200},
    ]:
        data = _get("/markets", params)
        if data:
            markets_raw = data.get("markets", []) if isinstance(data, dict) else []
            if markets_raw:
                break

    out = []
    for m in markets_raw:
        title      = (m.get("title") or "").lower()
        category   = (m.get("category") or "").lower()
        tags       = [t.lower() for t in (m.get("tags") or [])]
        close_time = m.get("close_time", "")

        # Block futures and politics regardless
        if any(pat in title for pat in _KALSHI_FUTURES):
            continue

        # Only single-game markets (ends within 48 h)
        if not _kalshi_is_game_day(close_time):
            continue

        is_sports = (
            "sports" in category
            or any(kw in title for kw in _SPORTS_KEYWORDS)
            or any(kw in " ".join(tags) for kw in _SPORTS_KEYWORDS)
        )
        if not is_sports:
            continue

        yes_bid = (m.get("yes_bid") or 0) / 100
        yes_ask = (m.get("yes_ask") or 0) / 100
        no_bid  = (m.get("no_bid")  or 0) / 100
        no_ask  = (m.get("no_ask")  or 0) / 100

        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid

        # Detect sport from title
        sport_key = ""
        for sk, tag_list in _SPORT_TAGS.items():
            if any(t.lower() in title for t in tag_list):
                sport_key = sk
                break

        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "category":     category,
            "tags":         tags,
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "result":       m.get("result", ""),
            "sport_key":    sport_key,
            "source":       "kalshi",
        })

    logger.info("Kalshi: %d sports markets fetched (from %d total)", len(out), len(markets_raw))
    return out


def _prob_to_american(prob: float) -> int:
    """Convert implied probability (0-1) to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    return int(100 * (1 - prob) / prob)


def get_event_markets(event_ticker: str) -> list[dict]:
    """
    Fetch all sub-markets for a Kalshi event (player props, game props, spreads, totals).
    event_ticker is the event-level ticker e.g. 'FIFA-WCSF-FRAVEN-20260616'.
    """
    data = _get(f"/events/{event_ticker}")
    if not data:
        return []
    markets = (data.get("event") or {}).get("markets", []) or data.get("markets", [])
    out = []
    for m in markets:
        yes_bid = (m.get("yes_bid") or 0) / 100
        yes_ask = (m.get("yes_ask") or 0) / 100
        no_bid  = (m.get("no_bid")  or 0) / 100
        no_ask  = (m.get("no_ask")  or 0) / 100
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid
        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "source":       "kalshi",
        })
    return out


def get_sports_events(limit: int = 500) -> list[dict]:
    """
    Fetch active sports markets from Kalshi using /markets endpoint with pagination.
    Covers MLB, soccer (FIFA Club World Cup, MLS, etc.), player props, game props.
    Returns flat list of markets sorted by volume.
    """
    all_markets: list[dict] = []
    cursor = None

    # Paginate through /markets until we have enough or run out
    for _ in range(10):
        params: dict = {"limit": 100, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        if not data or not isinstance(data, dict):
            break
        batch = data.get("markets", [])
        if not batch:
            break
        all_markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or len(all_markets) >= limit:
            break

    logger.info("Kalshi /markets: %d total fetched", len(all_markets))

    out = []
    for m in all_markets:
        title = (m.get("title") or "").lower()

        # Sports keyword filter
        is_sports = (
            any(kw in title for kw in _SPORTS_KEYWORDS)
            or (m.get("category") or "").lower() == "sports"
        )
        if not is_sports:
            continue

        # Block long-term futures
        if any(pat in title for pat in _KALSHI_FUTURES):
            continue

        yes_bid = (m.get("yes_bid") or 0) / 100
        yes_ask = (m.get("yes_ask") or 0) / 100
        no_bid  = (m.get("no_bid")  or 0) / 100
        no_ask  = (m.get("no_ask")  or 0) / 100
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid
        if not yes_mid:
            continue

        out.append({
            "market_id":    m.get("ticker", ""),
            "event_ticker": m.get("event_ticker", ""),
            "event_title":  m.get("title", ""),
            "title":        m.get("title", ""),
            "category":     (m.get("category") or "").lower(),
            "tags":         [t.lower() for t in (m.get("tags") or [])],
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "source":       "kalshi",
        })

    out.sort(key=lambda x: x["volume"], reverse=True)
    logger.info("Kalshi: %d sports markets fetched (from %d total)", len(out), len(all_markets))
    return out
