"""
Prediction Market Worker — Kalshi vs Polymarket live scanner.

Every scan:
1. Fetch ALL open/live markets from both Kalshi and Polymarket
2. Match markets across platforms by event title fuzzy-matching
3. Compare implied probabilities — find the better-value side
4. Track price movement in Redis (pre-game AND in-game prices move)
5. Fire Discord alerts on:
   - Significant price movement (5%+ swing on either platform)
   - Cross-platform divergence (platforms disagree by 8%+ = edge)
   - New live market detected for an active game

Runs every 3 minutes so in-game price swings are caught quickly.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Minimum price move to trigger a movement alert (absolute, 0-1 scale)
_MOVE_THRESHOLD   = 0.05   # 5% swing
# Minimum cross-platform divergence to highlight one as better value
_DIVERGE_THRESHOLD = 0.08  # 8% gap between Kalshi and Polymarket on same event

# Redis key prefix and TTL
_PRICE_KEY   = "predmkt:prices"   # hash: market_id → {yes, no, ts}
_ALERTED_KEY = "predmkt:alerted"  # set: keys already alerted this hour


def _redis():
    from src.core.config import REDIS_URL
    import redis as _r
    return _r.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)


# ── Fuzzy event matching ───────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    """Lowercase word tokens, strip punctuation."""
    import re
    return set(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())


def _match_score(title_a: str, title_b: str) -> float:
    """Jaccard similarity between two market titles (0-1)."""
    ta, tb = _tokens(title_a), _tokens(title_b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_match(market: dict, pool: list[dict], min_score: float = 0.35) -> dict | None:
    """Find the best-matching market from pool by title similarity."""
    best_score, best = 0.0, None
    title = market.get("title", "")
    for candidate in pool:
        score = _match_score(title, candidate.get("title", ""))
        if score > best_score:
            best_score, best = score, candidate
    return best if best_score >= min_score else None


# ── Price movement tracking ────────────────────────────────────────────────────

def _load_prev_price(r, key: str) -> dict | None:
    raw = r.hget(_PRICE_KEY, key)
    return json.loads(raw) if raw else None


def _save_price(r, key: str, yes: float, no: float) -> None:
    import time
    r.hset(_PRICE_KEY, key, json.dumps({"yes": yes, "no": no, "ts": time.time()}))
    r.expire(_PRICE_KEY, 86400)  # 24h TTL


def _detect_movement(prev: dict | None, yes: float, no: float) -> dict | None:
    """Returns movement dict if price moved more than threshold, else None."""
    if not prev:
        return None
    dy = abs(yes - prev["yes"])
    dn = abs(no  - prev["no"])
    move = max(dy, dn)
    if move < _MOVE_THRESHOLD:
        return None
    direction = "YES" if dy >= dn else "NO"
    old_price  = prev["yes"] if direction == "YES" else prev["no"]
    new_price  = yes         if direction == "YES" else no
    return {
        "direction": direction,
        "old_price": old_price,
        "new_price": new_price,
        "delta":     round(new_price - old_price, 4),
        "move_pct":  round(move * 100, 1),
    }


# ── Discord embeds ─────────────────────────────────────────────────────────────

def _pct(p: float | None) -> str:
    return f"{round((p or 0) * 100, 1)}%" if p is not None else "—"


def _american(p: float | None) -> str:
    if not p or p <= 0 or p >= 1:
        return "—"
    if p >= 0.5:
        return f"{int(-100 * p / (1 - p))}"
    return f"+{int(100 * (1 - p) / p)}"


def _trend_arrow(delta: float) -> str:
    if delta > 0.03:  return "🚀"
    if delta > 0:     return "📈"
    if delta < -0.03: return "💥"
    if delta < 0:     return "📉"
    return "➡️"


def _build_comparison_embed(
    kalshi: dict,
    poly:   dict,
    movement_k: dict | None,
    movement_p: dict | None,
) -> dict:
    """Build a Discord embed comparing Kalshi vs Polymarket for the same event."""
    title_display = kalshi.get("title") or poly.get("title") or "Unknown Event"
    sport = (kalshi.get("sport_key") or poly.get("sport_key") or "").replace("_", " ").upper()

    ky = kalshi.get("yes_price") or 0
    py = poly.get("yes_price")   or 0
    kn = kalshi.get("no_price")  or 0
    pn = poly.get("no_price")    or 0

    diverge = abs(ky - py)

    # Which platform has better YES value (higher price = market likes YES more)
    # Lower price on one platform = that side is being underpriced = value bet
    # Determine which platform offers better YES value
    # Also compare against HardRock (standard sportsbook) where applicable
    if diverge >= _DIVERGE_THRESHOLD:
        if ky > py:
            better_platform = "KALSHI"
            better_price    = ky
            worse_price     = py
            color = 0x1565C0
        else:
            better_platform = "POLYMARKET"
            better_price    = py
            worse_price     = ky
            color = 0x6A1B9A

        rec = (
            f"🏆 **PLAY {better_platform}** — YES at **{_pct(better_price)}** "
            f"vs {_pct(worse_price)} on the other platform\n"
            f"📍 For the same game on HardRock, compare their moneyline odds — "
            f"if HardRock is also close to {_pct(better_price)}, skip prediction markets "
            f"and use HardRock. If HardRock is worse, {better_platform} is your play."
        )
    else:
        rec = "Prices are aligned across platforms — no edge between Kalshi and Polymarket"
        color = 0x424242

    # Movement notes
    move_lines = []
    if movement_k:
        arr = _trend_arrow(movement_k["delta"])
        move_lines.append(
            f"Kalshi {arr} {movement_k['direction']} moved "
            f"{_pct(movement_k['old_price'])} → **{_pct(movement_k['new_price'])}** "
            f"({'+' if movement_k['delta'] > 0 else ''}{movement_k['move_pct']}%)"
        )
    if movement_p:
        arr = _trend_arrow(movement_p["delta"])
        move_lines.append(
            f"Polymarket {arr} {movement_p['direction']} moved "
            f"{_pct(movement_p['old_price'])} → **{_pct(movement_p['new_price'])}** "
            f"({'+' if movement_p['delta'] > 0 else ''}{movement_p['move_pct']}%)"
        )

    vol_k = f"${int(kalshi.get('volume', 0)):,}" if kalshi.get("volume") else "—"
    vol_p = f"${int(poly.get('volume', 0)):,}"   if poly.get("volume")   else "—"

    fields = [
        {
            "name":  "Kalshi",
            "value": (
                f"YES: **{_pct(ky)}** ({_american(ky)})\n"
                f"NO:  **{_pct(kn)}** ({_american(kn)})\n"
                f"Vol: {vol_k}"
            ),
        },
        {
            "name":  "Polymarket",
            "value": (
                f"YES: **{_pct(py)}** ({_american(py)})\n"
                f"NO:  **{_pct(pn)}** ({_american(pn)})\n"
                f"Vol: {vol_p}"
            ),
        },
        {
            "name":   "Platform Gap",
            "value":  f"**{round(diverge * 100, 1)}%** divergence",
            "inline": False,
        },
        {
            "name":   "Recommendation",
            "value":  rec,
            "inline": False,
        },
    ]
    if move_lines:
        fields.append({
            "name":   "Price Movement",
            "value":  "\n".join(move_lines),
            "inline": False,
        })

    return {
        "title":       f"📊 Prediction Market: {title_display[:200]}",
        "description": f"**{sport}** — Live price comparison",
        "color":       color,
        "fields":      fields,
    }


def _build_movement_only_embed(market: dict, movement: dict, platform: str) -> dict:
    """Embed for a significant price move on a single platform with no match found."""
    title_display = market.get("title", "Unknown")[:200]
    sport = (market.get("sport_key") or "").replace("_", " ").upper()
    arr   = _trend_arrow(movement["delta"])
    color = 0xE65100 if movement["delta"] > 0 else 0x1565C0

    platform_emoji = "🔵" if platform == "kalshi" else "🟣"
    return {
        "title":       f"{arr} Price Move: {title_display}",
        "description": (
            f"{platform_emoji} **{platform.title()}** — {sport}\n"
            f"{movement['direction']} price: {_pct(movement['old_price'])} → "
            f"**{_pct(movement['new_price'])}** ({'+' if movement['delta'] > 0 else ''}{movement['move_pct']}%)"
        ),
        "color":       color,
        "fields": [
            {"name": "YES", "value": _pct(market.get("yes_price")), "inline": True},
            {"name": "NO",  "value": _pct(market.get("no_price")),  "inline": True},
            {"name": "Volume", "value": f"${int(market.get('volume', 0)):,}", "inline": True},
        ],
    }


# ── Main scan ──────────────────────────────────────────────────────────────────

def scan_prediction_markets() -> dict:
    """
    Fetch all live/open markets from Kalshi + Polymarket, compare prices,
    detect movement, and fire Discord alerts.
    """
    try:
        from src.apis.kalshi import get_sports_markets
        from src.apis.polymarket import get_sports_markets as poly_get_sports
        from concurrent.futures import ThreadPoolExecutor, as_completed

        r = _redis()
        embeds_to_post = []

        # Fetch both platforms in parallel
        kalshi_markets: list[dict] = []
        poly_markets:   list[dict] = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            k_fut = pool.submit(get_sports_markets)
            p_fut = pool.submit(poly_get_sports)
            for fut in as_completed([k_fut, p_fut], timeout=20):
                try:
                    result = fut.result()
                    if fut is k_fut:
                        kalshi_markets = result or []
                    else:
                        poly_markets   = result or []
                except Exception as e:
                    logger.warning("Prediction market fetch failed: %s", e)

        logger.info(
            "Prediction markets: Kalshi=%d Polymarket=%d",
            len(kalshi_markets), len(poly_markets),
        )

        matched_poly_ids: set[str] = set()

        # ── 1. Process Kalshi markets — try to match each to Polymarket ───────
        for km in kalshi_markets:
            kid = km.get("market_id", "")
            ky  = km.get("yes_price") or 0
            kn  = km.get("no_price")  or 0
            if not ky and not kn:
                continue

            k_key    = f"kalshi:{kid}"
            prev_k   = _load_prev_price(r, k_key)
            movement_k = _detect_movement(prev_k, ky, kn)
            _save_price(r, k_key, ky, kn)

            # Try to find a matching Polymarket market
            pm = _find_match(km, poly_markets)
            if pm:
                pid = pm.get("market_id", "")
                matched_poly_ids.add(pid)
                py  = pm.get("yes_price") or 0
                pn  = pm.get("no_price")  or 0

                p_key    = f"poly:{pid}"
                prev_p   = _load_prev_price(r, p_key)
                movement_p = _detect_movement(prev_p, py, pn)
                _save_price(r, p_key, py, pn)

                diverge   = abs(ky - py)
                alert_key = f"cmp:{kid}:{pid}"
                already   = r.sismember(_ALERTED_KEY, alert_key)

                should_alert = (
                    diverge >= _DIVERGE_THRESHOLD
                    or movement_k is not None
                    or movement_p is not None
                )
                if should_alert and not already:
                    embed = _build_comparison_embed(km, pm, movement_k, movement_p)
                    embeds_to_post.append(embed)
                    r.sadd(_ALERTED_KEY, alert_key)
                    r.expire(_ALERTED_KEY, 3600)  # reset alert guard hourly

            elif movement_k:
                # No match on Polymarket — still alert if Kalshi price moved
                alert_key = f"move:k:{kid}"
                if not r.sismember(_ALERTED_KEY, alert_key):
                    embed = _build_movement_only_embed(km, movement_k, "kalshi")
                    embeds_to_post.append(embed)
                    r.sadd(_ALERTED_KEY, alert_key)
                    r.expire(_ALERTED_KEY, 3600)

        # ── 2. Process unmatched Polymarket markets ────────────────────────────
        for pm in poly_markets:
            pid = pm.get("market_id", "")
            if pid in matched_poly_ids:
                continue
            py = pm.get("yes_price") or 0
            pn = pm.get("no_price")  or 0
            if not py and not pn:
                continue

            p_key    = f"poly:{pid}"
            prev_p   = _load_prev_price(r, p_key)
            movement_p = _detect_movement(prev_p, py, pn)
            _save_price(r, p_key, py, pn)

            if movement_p:
                alert_key = f"move:p:{pid}"
                if not r.sismember(_ALERTED_KEY, alert_key):
                    embed = _build_movement_only_embed(pm, movement_p, "polymarket")
                    embeds_to_post.append(embed)
                    r.sadd(_ALERTED_KEY, alert_key)
                    r.expire(_ALERTED_KEY, 3600)

        # ── 3. Post all alerts ─────────────────────────────────────────────────
        if embeds_to_post:
            import asyncio
            from src.discord_bot.bot import _post
            for embed in embeds_to_post[:10]:  # max 10 per scan to avoid spam
                try:
                    asyncio.run(_post({"embeds": [embed]}))
                except Exception as e:
                    logger.error("Failed to post prediction market alert: %s", e)

        logger.info(
            "Prediction market scan done — %d alerts fired (K=%d P=%d matched=%d)",
            len(embeds_to_post), len(kalshi_markets), len(poly_markets), len(matched_poly_ids),
        )
        return {
            "kalshi":   len(kalshi_markets),
            "polymarket": len(poly_markets),
            "matched":  len(matched_poly_ids),
            "alerts":   len(embeds_to_post),
        }

    except Exception as exc:
        logger.error("Prediction market scan failed: %s", exc)
        return {"error": str(exc)}
