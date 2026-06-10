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
    "nba", "nfl", "mlb", "nhl", "ufc", "mma",
    "champions league", "premier league", "mls",
    "wimbledon", "us open", "french open", "masters", "pga",
    "formula 1", "f1", "ncaa",
    # single-game terms
    "game ", "match", "points", "score", "innings", "quarter",
    "heat", "celtics", "lakers", "warriors", "knicks", "nuggets",
    "yankees", "dodgers", "mets", "red sox", "cubs", "astros",
    "oilers", "panthers", "rangers", "avalanche", "lightning",
    "basketball", "baseball", "hockey", "soccer", "football",
    "total ", "over ", "under ",
]

# Futures / politics patterns — always block
_KALSHI_FUTURES = [
    "win the", "win the 2", "world cup", "fifa", "championship", "champion",
    "stanley cup", "super bowl", "world series", "nba finals", "march madness",
    "advance to", "qualify for", "make the playoffs", "make playoffs",
    "win series", "win title", "presidential", "election", "president",
    "primary", "governor", "senate", "congress", "bitcoin", "crypto",
    "by end of", "before ", "next year", "in 202",
]


def _kalshi_is_game_day(close_time: str) -> bool:
    """Return True only if market closes within 48 hours."""
    if not close_time:
        return False
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return timedelta(0) <= (dt - now) <= timedelta(hours=48)
    except Exception:
        return False


def get_sports_markets() -> list[dict]:
    """
    Fetch active SINGLE-GAME sports markets from Kalshi (closes within 48 h).
    Excludes tournament futures and politics.
    """
    data = _get("/markets", {"limit": 200, "status": "open"})
    if not data:
        return []

    markets_raw = data.get("markets", []) if isinstance(data, dict) else []
    if not markets_raw:
        data2 = _get("/markets", {"limit": 200})
        markets_raw = (data2 or {}).get("markets", []) if isinstance(data2, dict) else []

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
