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
    from src.apis.kalshi import get_sports_markets
    from src.apis.polymarket import get_sports_markets as poly_get
    from concurrent.futures import ThreadPoolExecutor, as_completed

    kalshi, poly = [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        k_fut = pool.submit(get_sports_markets)
        p_fut = pool.submit(poly_get)
        for fut in as_completed([k_fut, p_fut], timeout=20):
            try:
                result = fut.result() or []
                if fut is k_fut:
                    kalshi = result
                else:
                    poly = result
            except Exception as e:
                logger.warning("Prediction market fetch error: %s", e)
    return kalshi, poly


def _build_entry(kalshi_markets: list[dict], poly_markets: list[dict], max_picks: int = 5) -> list[dict]:
    """
    Match markets across platforms, pick the best-value platform per game,
    return top picks sorted by volume (most liquid = most reliable price signal).
    """
    picks = []
    seen_titles: set[str] = set()

    for km in kalshi_markets:
        if not km.get("yes_price"):
            continue
        title = km.get("title", "")
        title_key = " ".join(sorted(_tokens(title)))
        if title_key in seen_titles:
            continue

        pm = _best_match(km, poly_markets)

        if pm:
            platform, best = _better_platform(km, pm)
            seen_titles.add(title_key)
            # Mark poly match so it doesn't get processed again as standalone
            pm["_matched"] = True
        else:
            platform, best = "kalshi", km
            seen_titles.add(title_key)

        vol = float(best.get("volume") or 0)
        picks.append({
            "title":     title,
            "platform":  platform,
            "market":    best,
            "kalshi":    km,
            "poly":      pm,
            "volume":    vol,
            "sport_key": km.get("sport_key") or (pm.get("sport_key") if pm else ""),
            "yes_price": float(best.get("yes_price") or 0),
            "no_price":  float(best.get("no_price")  or 0),
        })

    # Also add Polymarket-only markets (no Kalshi match)
    for pm in poly_markets:
        if pm.get("_matched") or not pm.get("yes_price"):
            continue
        title = pm.get("title", "")
        title_key = " ".join(sorted(_tokens(title)))
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        picks.append({
            "title":     title,
            "platform":  "polymarket",
            "market":    pm,
            "kalshi":    None,
            "poly":      pm,
            "volume":    float(pm.get("volume") or 0),
            "sport_key": pm.get("sport_key") or "",
            "yes_price": float(pm.get("yes_price") or 0),
            "no_price":  float(pm.get("no_price")  or 0),
        })

    # Sort by volume descending — highest liquidity = most reliable price
    picks.sort(key=lambda x: x["volume"], reverse=True)
    return picks[:max_picks]


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

    period_label = "DAY" if period == "day" else "NIGHT"
    rows = []

    for i, pick in enumerate(picks, 1):
        emoji    = _PLATFORM_EMOJI[pick["platform"]]
        platform = _PLATFORM_LABEL[pick["platform"]]
        title    = pick["title"][:60]
        yes_pct  = _pct(pick["yes_price"])
        no_pct   = _pct(pick["no_price"])
        yes_am   = _american(pick["yes_price"])
        no_am    = _american(pick["no_price"])
        vol      = f"${int(pick['volume']):,}" if pick["volume"] else "—"
        sport    = (pick.get("sport_key") or "").split("_")[-1].upper()

        # Show both platforms if matched
        both_line = ""
        if pick["kalshi"] and pick["poly"]:
            ky = _pct(pick["kalshi"].get("yes_price"))
            py = _pct(pick["poly"].get("yes_price"))
            both_line = f"\n`Kalshi {ky}  |  Polymarket {py}`  → best: **{platform}**"

        rows.append(
            f"**{i}. {title}** `{sport}`\n"
            f"{emoji} **{platform}** — YES {yes_pct} ({yes_am})  |  NO {no_pct} ({no_am})\n"
            f"Volume: {vol}{both_line}"
        )

    description = "\n\n".join(rows)

    embed = {
        "title":       f"📊 Prediction Market Entry — {period_label}",
        "description": (
            f"Best odds across Kalshi + Polymarket — **{len(picks)} pick{'s' if len(picks) > 1 else ''}**\n"
            f"Place on the listed platform. Cross-check HardRock for same game.\n\n"
            + description
        ),
        "color": 0x4A148C,  # deep purple
        "footer": {"text": "Kalshi 🔵  Polymarket 🟣  — prices update every 3 min"},
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

        picks = _build_entry(kalshi, poly, max_picks=5)
        if not picks:
            logger.info("Prediction market %s entry: no qualifying picks", period)
            return {"picks": 0, "posted": False}

        _post_prediction_entry(period, picks)
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

        logger.info(
            "Prediction market scan: K=%d P=%d moves=%d",
            len(kalshi), len(poly), alerts,
        )
        return {"kalshi": len(kalshi), "polymarket": len(poly), "alerts": alerts}

    except Exception as exc:
        logger.error("Prediction market scan failed: %s", exc)
        return {"error": str(exc)}
