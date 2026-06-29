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

        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)
        no_bid  = float(m.get("no_bid_dollars")  or m.get("no_bid")  or 0)
        no_ask  = float(m.get("no_ask_dollars")  or m.get("no_ask")  or 0)
        last    = float(m.get("last_price_dollars") or m.get("last_price") or 0)
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid or last
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid or (1 - yes_mid if yes_mid else 0)
        if yes_mid > 1: yes_mid /= 100
        if no_mid  > 1: no_mid  /= 100

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
    # Men's leagues
    "nba", "nfl", "mlb", "nhl", "ufc", "mma",
    "champions league", "premier league", "mls", "liga", "serie a",
    "wimbledon", "us open", "french open", "masters", "pga", "atp",
    "formula 1", "f1", "ncaa", "fifa", "world cup", "copa", "bundesliga",
    # Women's leagues
    "wnba", "nwsl", "pwhl", "lpga", "wta",
    "women's", "womens", "ncaaw",
    # single-game terms
    "game ", "match", "points", "score", "innings", "quarter",
    "goals", "goal", "runs", "sets", "aces",
    # US teams (men's)
    "heat", "celtics", "lakers", "warriors", "knicks", "nuggets",
    "yankees", "dodgers", "mets", "red sox", "cubs", "astros",
    "orioles", "baltimore", "san diego", "kansas city", "detroit",
    "tampa bay", "atlanta", "los angeles",
    "oilers", "panthers", "rangers", "avalanche", "lightning",
    # WNBA teams
    "fever", "liberty", "aces", "sun", "lynx", "sky", "storm", "mystics",
    "wings", "dream", "sparks", "mercury",
    # World soccer teams/tournaments
    "england", "portugal", "brazil", "france", "germany", "spain",
    "argentina", "morocco", "algeria", "colombia", "croatia",
    "both teams to score", "btts",
    "basketball", "baseball", "hockey", "soccer", "football",
    "tennis", "golf", "racing", "boxing", "nascar",
    "total ", "over ", "under ", "wins by",
    # Player/team props
    "hits", "home run", "strikeout", "rbi", "stolen base",
    "points", "rebounds", "assists", "blocks", "steals", "threes",
    "passing yards", "rushing yards", "receiving yards", "touchdowns",
    "saves", "shots on goal", "goalkeeper",
    "aces", "double faults", "break points",
    "birdies", "bogeys", "eagles",
    "knockdown", "round ", "decision", "ko", "tko", "submission",
    "laps led", "fastest lap", "pole position",
    "player ", "will ", "score more than", "record ",
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

        if not yes_mid and not no_mid:
            continue  # no valid price on either side — skip illiquid market

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
    Fetch active sports markets from Kalshi.
    Queries known sports series tickers directly (most reliable) then falls
    back to paginated /markets scan. Covers MLB, WNBA, FIFA CWC, MLS, NFL, NBA, NHL, tennis.
    Returns flat list of markets sorted by volume.
    """
    # Known Kalshi sports series tickers — query each directly for best results
    _SERIES = [
        # ── Baseball ──────────────────────────────────────────────
        "KXMLBGAME",      # MLB game winners
        "KXMLBTOTAL",     # MLB run totals
        "KXMLBHITS",      # MLB player hits props
        "KXMLBHR",        # MLB home runs props
        "KXMLBRBI",       # MLB RBI props
        "KXMLBSO",        # MLB strikeouts props (pitcher)
        "KXMLBTEAM",      # MLB team props
        # ── Soccer (Men's) ────────────────────────────────────────
        "KXWCGAME",       # FIFA Club World Cup game winners
        "KXWCTOTAL",      # FIFA Club World Cup goal totals
        "KXWCGOAL",       # FIFA CWC goalscorer props
        "KXWCTEAM",       # FIFA CWC team props
        "KXMLSGAME",      # MLS game winners
        "KXMLSTOTAL",     # MLS totals
        "KXMLSGOAL",      # MLS goalscorer props
        "KXEPLGAME",      # English Premier League
        "KXEPLTOTAL",     # EPL totals
        "KXEPLGOAL",      # EPL goalscorer props
        "KXUEFAGAME",     # UEFA Champions League
        "KXUEFATOTAL",    # UEFA totals
        "KXUEFAGOAL",     # UEFA goalscorer props
        "KXLALIGAGAME",   # La Liga
        "KXBUNDESLIGAGAME", # Bundesliga
        "KXSERIEAGAME",   # Serie A
        # ── Soccer (Women's) ──────────────────────────────────────
        "KXNWSLGAME",     # NWSL game winners
        "KXNWSLTOTAL",    # NWSL totals
        "KXNWSLGOAL",     # NWSL goalscorer props
        "KXWWCGAME",      # FIFA Women's World Cup
        "KXWWCTOTAL",     # FIFA Women's WC totals
        # ── Basketball (Men's) ────────────────────────────────────
        "KXNBAGAME",      # NBA game winners
        "KXNBATOTAL",     # NBA totals
        "KXNBAPOINTS",    # NBA player points props
        "KXNBAREBOUNDS",  # NBA player rebounds props
        "KXNBAASSISTS",   # NBA player assists props
        "KXNBATEAM",      # NBA team props
        "KXNCAABGAME",    # NCAA Men's Basketball
        # ── Basketball (Women's) ──────────────────────────────────
        "KXWNBAGAME",     # WNBA game winners
        "KXWNBATOTAL",    # WNBA totals
        "KXWNBAPOINTS",   # WNBA player points props
        "KXWNBAREBOUNDS", # WNBA player rebounds props
        "KXWNBAASSISTS",  # WNBA player assists props
        "KXWNBATEAM",     # WNBA team props
        "KXNCAAWGAME",    # NCAA Women's Basketball
        # ── American Football ─────────────────────────────────────
        "KXNFLGAME",      # NFL game winners
        "KXNFLTOTAL",     # NFL totals
        "KXNFLPASS",      # NFL passing yards props
        "KXNFLRUSH",      # NFL rushing yards props
        "KXNFLREC",       # NFL receiving yards props
        "KXNFLTD",        # NFL touchdown props
        "KXNFLTEAM",      # NFL team props
        "KXNCAAFGAME",    # College Football
        # ── Hockey ────────────────────────────────────────────────
        "KXNHLGAME",      # NHL game winners
        "KXNHLTOTAL",     # NHL totals
        "KXNHLGOAL",      # NHL goalscorer props
        "KXNHLSAVES",     # NHL goalie saves props
        "KXNHLTEAM",      # NHL team props
        "KXPWHLGAME",     # PWHL (women's hockey)
        "KXPWHLTOTAL",    # PWHL totals
        # ── Tennis (Men's & Women's) ──────────────────────────────
        "KXTENNIS",       # Tennis general
        "KXATPGAME",      # ATP (men's tennis)
        "KXATPSETS",      # ATP sets props
        "KXWTGAME",       # WTA (women's tennis)
        "KXWTSETS",       # WTA sets props
        # ── Golf (Men's & Women's) ────────────────────────────────
        "KXGOLF",         # Golf general
        "KXPGAGAME",      # PGA Tour
        "KXPGACUTS",      # PGA cut props
        "KXLPGAGAME",     # LPGA Tour (women's golf)
        "KXLPGACUTS",     # LPGA cut props
        # ── Combat Sports ─────────────────────────────────────────
        "KXUFC",          # UFC/MMA
        "KXUFCMETHOD",    # UFC method of victory props
        "KXBOXGAME",      # Boxing
        "KXBOXMETHOD",    # Boxing method of victory props
        # ── Racing ────────────────────────────────────────────────
        "KXF1GAME",       # Formula 1
        "KXF1DRIVER",     # F1 driver props
        "KXNASCARGAME",   # NASCAR
        "KXNASCARDRIVER", # NASCAR driver props
    ]

    all_markets: list[dict] = []
    seen_tickers: set[str] = set()

    # 1. Direct series queries — most reliable, hits the right markets immediately
    for series in _SERIES:
        data = _get("/markets", {"limit": 100, "status": "open", "series_ticker": series})
        if data and isinstance(data, dict):
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)

    logger.info("Kalshi series fetch: %d markets from %d series", len(all_markets), len(_SERIES))

    # 2. Fallback: paginated scan catches anything not in known series
    if len(all_markets) < 20:
        cursor = None
        for _ in range(5):
            params: dict = {"limit": 100, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            data = _get("/markets", params)
            if not data or not isinstance(data, dict):
                break
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)
            cursor = data.get("cursor")
            if not cursor:
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

        # Filter by expected_expiration_time — active window: 5 AM to 3 AM ET
        # Include games from 3h ago through next 3 AM ET cutoff
        exp_raw = m.get("expected_expiration_time") or m.get("expiration_time") or ""
        if exp_raw:
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                import zoneinfo as _zi
                _ET      = _zi.ZoneInfo("America/New_York")
                _exp_utc = _dt.fromisoformat(exp_raw.replace("Z", "+00:00"))
                _now_utc = _dt.now(_tz.utc)
                _now_et  = _now_utc.astimezone(_ET)
                # Next 3 AM ET cutoff
                _cutoff_et = _now_et.replace(hour=3, minute=0, second=0, microsecond=0)
                if _cutoff_et <= _now_et:
                    _cutoff_et = _cutoff_et + _td(days=1)  # tomorrow 3 AM
                _cutoff_utc = _cutoff_et.astimezone(_tz.utc)
                if _exp_utc < _now_utc - _td(hours=3):
                    continue  # game ended more than 3h ago
                if _exp_utc > _cutoff_utc:
                    continue  # past tonight's 3 AM cutoff
            except Exception:
                pass  # include if unparseable

        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)
        no_bid  = float(m.get("no_bid_dollars")  or m.get("no_bid")  or 0)
        no_ask  = float(m.get("no_ask_dollars")  or m.get("no_ask")  or 0)
        last    = float(m.get("last_price_dollars") or m.get("last_price") or 0)

        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid or last
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid  or (1 - yes_mid if yes_mid else 0)

        # If prices are in cents (0-100) scale, normalise to 0-1
        if yes_mid > 1:
            yes_mid /= 100
        if no_mid > 1:
            no_mid /= 100

        if not yes_mid:
            continue

        vol = float(m.get("volume_24h_fp") or m.get("volume_fp") or m.get("volume") or 0)

        subtitle = (m.get("subtitle") or "").strip()

        # Convert game_time and close_time from UTC → ET ISO string
        def _to_et(raw: str) -> str:
            if not raw:
                return ""
            try:
                from datetime import datetime as _dt2, timezone as _tz2
                import zoneinfo as _zi2
                _ET2 = _zi2.ZoneInfo("America/New_York")
                _dt_utc = _dt2.fromisoformat(raw.replace("Z", "+00:00"))
                if _dt_utc.tzinfo is None:
                    _dt_utc = _dt_utc.replace(tzinfo=_tz2.utc)
                return _dt_utc.astimezone(_ET2).isoformat()
            except Exception:
                return raw

        _expiration_raw = m.get("expected_expiration_time") or m.get("expiration_time") or ""
        _close_time_raw = m.get("close_time", "")

        out.append({
            "market_id":    m.get("ticker", ""),
            "event_ticker": m.get("event_ticker", ""),
            # subtitle = "Team A vs Team B" on most Kalshi sports markets
            "event_title":  subtitle or m.get("title", ""),
            "title":        m.get("title", ""),
            "subtitle":     subtitle,
            "category":     (m.get("category") or "").lower(),
            "tags":         [t.lower() for t in (m.get("tags") or [])],
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":            vol,
            # close_time = when Kalshi stops accepting bets = actual game start time
            # expected_expiration_time = when market settles (~2-3h AFTER game ends) — kept for filtering only
            "game_time":         _to_et(_close_time_raw),
            "expiration_time":   _to_et(_expiration_raw),
            "source":            "kalshi",
        })

    out.sort(key=lambda x: x["volume"], reverse=True)
    logger.info("Kalshi: %d sports markets fetched (from %d total)", len(out), len(all_markets))
    return out
