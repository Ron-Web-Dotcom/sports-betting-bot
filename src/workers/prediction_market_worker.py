"""
Prediction Market Worker — Kalshi vs Polymarket entry generator.

Mirrors the HardRock entry workflow but for prediction markets:
  - Scans Kalshi + Polymarket for ALL live/upcoming sports markets
  - For each game, picks whichever platform has the better odds (lower price = better value)
  - Posts a clean Discord entry: platform, game, YES/NO odds, recommendation
  - Runs at same times as HardRock entries (day: 10:30 AM, night: 4:30 PM ET)
  - Also polls every 3 min for in-game price moves on active entries

Two entries in Discord every day:
  1. HardRock entry  — standard sportsbook (ML/spread/total)
  2. Kalshi/Poly entry — prediction markets, best odds across both platforms
"""
import json
import logging

logger = logging.getLogger(__name__)

_MOVE_THRESHOLD = 0.05   # 5% price move triggers live alert
_PRICE_CACHE    = "predmkt:prices"
_ALERTED_CACHE  = "predmkt:alerted"
_ENTRY_HASH_KEY = "predmkt:entry_hash"


def _redis():
    from src.core.config import REDIS_URL
    import redis as _r
    return _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


# ── Fuzzy title matching ───────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    import re
    return set(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _best_match(market: dict, pool: list[dict], threshold: float = 0.30) -> dict | None:
    best_score, best = 0.0, None
    title = market.get("title", "")
    for candidate in pool:
        s = _similarity(title, candidate.get("title", ""))
        if s > best_score:
            best_score, best = s, candidate
    return best if best_score >= threshold else None


# ── Price helpers ──────────────────────────────────────────────────────────────

def _pct(p) -> str:
    return f"{round(float(p) * 100, 1)}%" if p else "—"


def _american(p) -> str:
    if not p or float(p) <= 0 or float(p) >= 1:
        return "—"
    p = float(p)
    if p >= 0.5:
        return f"{int(-100 * p / (1 - p))}"
    return f"+{int(100 * (1 - p) / p)}"


def _better_platform(km: dict, pm: dict) -> tuple[str, dict]:
    """
    Return which platform offers better value (lower YES price = you pay less
    for the same $1 payout = better odds for the bettor).
    Falls back to whichever has non-zero data.
    """
    ky = float(km.get("yes_price") or 0)
    py = float(pm.get("yes_price") or 0)
    if not ky:
        return "polymarket", pm
    if not py:
        return "kalshi", km
    # Lower price = better value (you're getting better odds)
    if ky <= py:
        return "kalshi", km
    return "polymarket", pm


# ── Build the entry ────────────────────────────────────────────────────────────

def _fetch_all_markets() -> tuple[list[dict], list[dict]]:
    """Kalshi only — Polymarket disabled (returns pop culture / futures noise)."""
    from src.apis.kalshi import get_sports_markets
    try:
        kalshi = get_sports_markets() or []
    except Exception as e:
        logger.warning("Kalshi fetch error: %s", e)
        kalshi = []
    return kalshi, []  # poly always empty


def _build_entry(kalshi_markets: list[dict], poly_markets: list[dict], max_picks: int = 1) -> list[dict]:
    """
    Use AI to score ALL today's Kalshi markets and return the single best pick.
    Only includes markets with genuine edge (yes_price 40-70% — not heavy faves or huge dogs).
    """
    import json
    from src.engines.ai_engine import _call_json

    # Filter to meaningful game markets — skip huge faves (>80%) and illiquid (<$500 vol)
    candidates = []
    seen: set[str] = set()
    for km in kalshi_markets:
        yes = float(km.get("yes_price") or 0)
        no  = float(km.get("no_price")  or 0)
        vol = float(km.get("volume")    or 0)
        title = km.get("title", "")
        title_key = " ".join(sorted(_tokens(title)))
        if title_key in seen or not yes:
            continue
        seen.add(title_key)
        # Skip extreme odds (>85% or <15%) — no edge there
        if yes > 0.85 or yes < 0.15:
            continue
        candidates.append(km)

    if not candidates:
        logger.info("Kalshi: no candidates after price filter")
        return []

    # Sort by volume for context — AI still scores all
    candidates.sort(key=lambda x: float(x.get("volume") or 0), reverse=True)
    top = candidates[:30]  # send top 30 by liquidity to AI

    system = """You are an elite prediction market analyst. Given a list of today's Kalshi
sports game markets, identify the SINGLE best bet with genuine edge.

Consider: implied probability vs true probability, market liquidity, sport knowledge,
team form, matchup context. Look for markets where the price is WRONG.

Return ONLY valid JSON:
{
  "index": <int — index in the input list>,
  "side": "yes" | "no",
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float e.g. 0.06 = 6% edge>,
  "reasoning": "<2-3 sentences explaining the edge>",
  "key_factors": ["<factor1>", "<factor2>"]
}

Only return a pick if confidence >= 0.60 and ev_pct >= 0.04.
If nothing qualifies, return {"index": null}.
"""

    market_list = [
        {
            "index":     i,
            "title":     m.get("title", ""),
            "yes_price": f"{round(float(m.get('yes_price',0))*100)}%",
            "no_price":  f"{round(float(m.get('no_price',0))*100)}%",
            "volume":    f"${int(float(m.get('volume',0))):,}",
            "sport":     (m.get("sport_key") or "").split("_")[-1].upper(),
            "closes":    m.get("close_time", ""),
        }
        for i, m in enumerate(top)
    ]

    prompt = (
        f"Today's Kalshi sports markets ({len(market_list)} games):\n\n"
        f"```json\n{json.dumps(market_list, indent=2)}\n```\n\n"
        f"Pick the single best bet with genuine edge."
    )

    try:
        result = _call_json(prompt, system)
    except Exception as e:
        logger.warning("Kalshi AI scoring failed: %s", e)
        result = None

    if not result or result.get("index") is None:
        logger.info("Kalshi AI: no qualifying pick found")
        return []

    idx        = result.get("index")
    confidence = float(result.get("confidence") or 0)
    ev_pct     = float(result.get("ev_pct") or 0)

    if idx is None or idx >= len(top) or confidence < 0.60 or ev_pct < 0.04:
        return []

    km   = top[idx]
    side = result.get("side", "yes").lower()
    yes  = float(km.get("yes_price") or 0)
    no   = float(km.get("no_price")  or 0)

    return [{
        "title":       km.get("title", ""),
        "platform":    "kalshi",
        "market":      km,
        "kalshi":      km,
        "poly":        None,
        "volume":      float(km.get("volume") or 0),
        "sport_key":   km.get("sport_key") or "",
        "yes_price":   yes,
        "no_price":    no,
        "side":        side,
        "confidence":  confidence,
        "ev_pct":      ev_pct,
        "reasoning":   result.get("reasoning", ""),
        "key_factors": result.get("key_factors", []),
    }]


# ── Discord embed ──────────────────────────────────────────────────────────────

_PLATFORM_EMOJI  = {"kalshi": "🔵", "polymarket": "🟣"}
_PLATFORM_LABEL  = {"kalshi": "Kalshi", "polymarket": "Polymarket"}


def _post_prediction_entry(period: str, picks: list[dict]) -> None:
    import asyncio, hashlib, json
    from src.discord_bot.bot import _post

    if not picks:
        return

    r = _redis()

    # Idempotency — only post if picks changed
    entry_hash = hashlib.md5(
        json.dumps([p["title"] + p["platform"] for p in picks]).encode()
    ).hexdigest()
    hash_key = f"{_ENTRY_HASH_KEY}:{period}"
    if r.get(hash_key) == entry_hash:
        logger.info("Prediction market %s entry unchanged — skipping post", period)
        return
    r.setex(hash_key, 7200, entry_hash)

    import hashlib
    from datetime import datetime
    import zoneinfo
    ET           = zoneinfo.ZoneInfo("America/New_York")
    now_et       = datetime.now(ET)
    date_str     = now_et.strftime("%b %-d, %Y")
    time_str     = now_et.strftime("%-I:%M %p ET")
    period_emoji = "☀️" if period == "day" else "🌙"
    period_label = "DAY" if period == "day" else "NIGHT"
    ticket_id    = hashlib.md5(f"pred{period}{date_str}".encode()).hexdigest()[:8].upper()

    pick     = picks[0]
    title    = pick["title"][:60]
    yes_pct  = _pct(pick["yes_price"])
    no_pct   = _pct(pick["no_price"])
    yes_am   = _american(pick["yes_price"])
    no_am    = _american(pick["no_price"])
    vol      = f"${int(pick['volume']):,}" if pick["volume"] else "—"
    sport    = (pick.get("sport_key") or "").split("_")[-1].upper() or "SPORTS"
    side     = (pick.get("side") or "yes").upper()
    conf     = f"{round((pick.get('confidence') or 0) * 100)}%"
    ev       = f"{round((pick.get('ev_pct') or 0) * 100, 1)}%"
    reasoning = pick.get("reasoning", "")
    factors  = pick.get("key_factors", [])
    factors_str = "  ·  ".join(f"**{f}**" for f in factors[:3]) if factors else "—"

    # Highlight the recommended side
    pick_line = (
        f"┣  YES **{yes_pct}** ({yes_am})  ◀ **BET THIS**" if side == "YES"
        else f"┣  YES **{yes_pct}** ({yes_am})"
    )
    no_line = (
        f"┣  NO  **{no_pct}** ({no_am})  ◀ **BET THIS**" if side == "NO"
        else f"┣  NO  **{no_pct}** ({no_am})"
    )

    embed = {
        "title": f"🎟️  KALSHI SLIP  ·  {period_emoji} {period_label}",
        "description": (
            f"```\n"
            f"  Ticket #{ticket_id}        {date_str}\n"
            f"  {time_str}         GAME OUTCOME\n"
            f"```"
        ),
        "fields": [
            {"name": "CONTRACT", "value": f"🔵 **Kalshi**  `{sport}`\n┣  **{title}**", "inline": False},
            {"name": "ODDS", "value": f"{pick_line}\n{no_line}", "inline": True},
            {"name": "VOLUME", "value": vol, "inline": True},
            {"name": "PLAY", "value": f"**{side}**  ·  {conf} confidence  ·  +{ev} edge", "inline": False},
            {"name": "REASONING", "value": reasoning[:300] if reasoning else "—", "inline": False},
            {"name": "KEY FACTORS", "value": factors_str, "inline": False},
            {"name": "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "value": f"🔵 Kalshi  ·  Price updates every 3 min  ·  Exit before final whistle", "inline": False},
        ],
        "color": 0x1B5E20,  # Kalshi dark green
    }

    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Prediction market %s entry posted (%d picks)", period, len(picks))
    except Exception as e:
        logger.error("Failed to post prediction market entry: %s", e)


# ── Live movement alerts (interval scan) ──────────────────────────────────────

def _load_price(r, key: str) -> dict | None:
    raw = r.hget(_PRICE_CACHE, key)
    return json.loads(raw) if raw else None


def _save_price(r, key: str, yes: float, no: float) -> None:
    import time
    r.hset(_PRICE_CACHE, key, json.dumps({"yes": yes, "no": no, "ts": time.time()}))
    r.expire(_PRICE_CACHE, 86400)


def _check_move(prev: dict | None, yes: float, no: float) -> dict | None:
    if not prev:
        return None
    dy = abs(yes - prev["yes"])
    dn = abs(no  - prev["no"])
    move = max(dy, dn)
    if move < _MOVE_THRESHOLD:
        return None
    side    = "YES" if dy >= dn else "NO"
    old_p   = prev["yes"] if side == "YES" else prev["no"]
    new_p   = yes         if side == "YES" else no
    delta   = new_p - old_p
    arrow   = "🚀" if delta > 0.03 else ("📈" if delta > 0 else ("💥" if delta < -0.03 else "📉"))
    return {"side": side, "old": old_p, "new": new_p, "delta": delta, "arrow": arrow, "move_pct": round(move * 100, 1)}


def _post_move_alert(market: dict, move: dict, platform: str) -> None:
    import asyncio
    from src.discord_bot.bot import _post
    emoji   = _PLATFORM_EMOJI.get(platform, "📊")
    title   = (market.get("title") or "")[:100]
    sport   = (market.get("sport_key") or "").split("_")[-1].upper()
    sign    = "+" if move["delta"] > 0 else ""
    embed = {
        "title":       f"{move['arrow']} Live Price Move — {title}",
        "description": (
            f"{emoji} **{_PLATFORM_LABEL.get(platform, platform)}**  `{sport}`\n"
            f"{move['side']} price: {_pct(move['old'])} → **{_pct(move['new'])}** "
            f"({sign}{move['move_pct']}%)\n\n"
            f"YES: **{_pct(market.get('yes_price'))}** ({_american(market.get('yes_price'))})\n"
            f"NO:  **{_pct(market.get('no_price'))}**  ({_american(market.get('no_price'))})"
        ),
        "color": 0xE65100 if move["delta"] > 0 else 0x1565C0,
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
    except Exception as e:
        logger.error("Move alert failed: %s", e)


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_prediction_market_day_entry() -> dict:
    """Post the day prediction market entry (runs at 10:30 AM ET alongside HardRock day entry)."""
    return _generate_entry("day")


def generate_prediction_market_night_entry() -> dict:
    """Post the night prediction market entry (runs at 4:30 PM ET alongside HardRock night entry)."""
    return _generate_entry("night")


def _generate_entry(period: str) -> dict:
    try:
        kalshi, poly = _fetch_all_markets()
        if not kalshi and not poly:
            logger.info("Prediction market %s entry: no markets available", period)
            return {"picks": 0, "posted": False}

        picks = _build_entry(kalshi, poly, max_picks=1)
        if not picks:
            logger.info("Prediction market %s entry: no qualifying picks", period)
            return {"picks": 0, "posted": False}

        _post_prediction_entry(period, picks)

        try:
            from src.workers.slip_tracker import save_slip
            save_slip(period, "kalshi", picks)
        except Exception as e:
            logger.warning("slip_tracker.save_slip failed: %s", e)

        return {"period": period, "picks": len(picks), "posted": True}
    except Exception as exc:
        logger.error("Prediction market %s entry failed: %s", period, exc)
        return {"error": str(exc)}


def scan_prediction_markets() -> dict:
    """
    Interval scan (every 3 min) — detects live price moves on active markets
    and fires alerts. Does NOT re-post the full entry.
    """
    try:
        r = _redis()
        kalshi, poly = _fetch_all_markets()
        alerts = 0

        all_markets = [(m, "kalshi") for m in kalshi] + [(m, "polymarket") for m in poly]

        for market, platform in all_markets:
            mid = market.get("market_id", "")
            if not mid:
                continue
            yes = float(market.get("yes_price") or 0)
            no  = float(market.get("no_price")  or 0)
            if not yes and not no:
                continue

            key  = f"{platform}:{mid}"
            prev = _load_price(r, key)
            move = _check_move(prev, yes, no)
            _save_price(r, key, yes, no)

            if move:
                alert_key = f"move:{key}"
                if not r.sismember(_ALERTED_CACHE, alert_key):
                    _post_move_alert(market, move, platform)
                    r.sadd(_ALERTED_CACHE, alert_key)
                    r.expire(_ALERTED_CACHE, 3600)
                    alerts += 1

        logger.info("Prediction market scan: K=%d moves=%d", len(kalshi), alerts)
        return {"kalshi": len(kalshi), "alerts": alerts}

    except Exception as exc:
        logger.error("Prediction market scan failed: %s", exc)
        return {"error": str(exc)}
