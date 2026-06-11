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

def _fetch_todays_games() -> list[dict]:
    """Pull today's games from Odds API snapshots in the DB."""
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        snapshots = get_latest_snapshots_by_game()
        games = {}
        for game_id, snaps in snapshots.items():
            if not snaps:
                continue
            s = snaps[0]
            home = s.get("home_team", "")
            away = s.get("away_team", "")
            sport = s.get("sport_key", "")
            if not home or not away:
                continue
            home_odds = next((x["best_odds"] for x in snaps
                              if x.get("market") == "h2h" and x.get("selection") == home), None)
            away_odds = next((x["best_odds"] for x in snaps
                              if x.get("market") == "h2h" and x.get("selection") == away), None)
            if not home_odds or not away_odds:
                continue

            def to_prob(o):
                o = int(o)
                return 100 / (100 + o) if o > 0 else abs(o) / (abs(o) + 100)

            hp = to_prob(home_odds)
            ap = to_prob(away_odds)
            total = hp + ap
            games[game_id] = {
                "game_id":   game_id,
                "title":     f"{away} vs {home}",
                "home_team": home,
                "away_team": away,
                "sport_key": sport,
                "commence":  s.get("commence_time", ""),
                "home_prob": round(hp / total, 4),
                "away_prob": round(ap / total, 4),
                "home_odds": home_odds,
                "away_odds": away_odds,
            }
        return list(games.values())
    except Exception as e:
        logger.warning("_fetch_todays_games failed: %s", e)
        return []


def _build_entry(kalshi_markets: list[dict], poly_markets: list[dict], max_picks: int = 1) -> list[dict]:
    """
    Use AI to score today's games from Odds API and return the single best
    pick formatted as a Kalshi-style YES/NO contract.
    kalshi_markets and poly_markets are accepted for signature compat but unused.
    """
    import json as _json
    from src.engines.ai_engine import _call_json

    games = _fetch_todays_games()
    if not games:
        logger.info("Kalshi entry: no games from Odds API")
        return []

    candidates = [g for g in games if 0.20 <= g["home_prob"] <= 0.80] or games

    system = """You are an elite sports analyst and prediction market researcher.

Given today's live games, do deep research on EACH game before deciding:
- Recent form (last 5-10 games, win/loss streak, home/away splits)
- Key injuries and lineup changes affecting this matchup
- Head-to-head history (last 5 meetings, recent series if playoffs)
- Pace, defensive/offensive ratings, matchup advantages
- Rest days, travel, back-to-back situations
- Market line vs your true probability estimate

After researching all games, identify the SINGLE best game where you have the highest
confidence edge — where the true probability clearly differs from the market price.

Return ONLY valid JSON:
{
  "index": <int>,
  "team": "<team name to back>",
  "question": "<Kalshi-style YES/NO question based on the actual game, e.g. 'Will [Team] win tonight?' or 'Will [Team] win Game [N]?' — use the real game number if it's a playoff series>",
  "true_prob": <float 0.0-1.0>,
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float e.g. 0.06 = 6% edge>,
  "reasoning": "<3-4 sentences covering recent form, key injury/matchup factor, and why the market is wrong>"
}

Only pick if confidence >= 0.65 and ev_pct >= 0.04. Return {"index": null} if nothing qualifies."""

    game_list = [
        {
            "index":      i,
            "game":       g["title"],
            "sport":      g["sport_key"].split("_")[-1].upper(),
            "home_team":  g["home_team"],
            "away_team":  g["away_team"],
            "home_win_%": f"{round(g['home_prob']*100)}%",
            "away_win_%": f"{round(g['away_prob']*100)}%",
            "home_odds":  f"{int(g['home_odds']):+d}",
            "away_odds":  f"{int(g['away_odds']):+d}",
            "game_time":  g.get("commence", ""),
        }
        for i, g in enumerate(candidates[:40])
    ]

    from datetime import datetime
    import zoneinfo
    today_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%A %B %-d, %Y")

    prompt = (
        f"Today is {today_str}. These are the games being played TODAY:\n\n"
        f"```json\n{_json.dumps(game_list, indent=2)}\n```\n\n"
        f"Research each game deeply using your training knowledge (injuries, form, H2H, "
        f"matchup edges, playoff context if applicable). Write the question based on the "
        f"ACTUAL game — use the real series game number if it's playoffs, do not guess. "
        f"Pick the single game where you have the most confident edge for a Kalshi YES/NO contract."
    )

    try:
        result = _call_json(prompt, system)
    except Exception as e:
        logger.warning("Kalshi AI scoring failed: %s", e)
        return []

    if not result or result.get("index") is None:
        logger.info("Kalshi AI: no qualifying pick")
        return []

    idx        = result.get("index", 0)
    confidence = float(result.get("confidence") or 0)
    ev_pct     = float(result.get("ev_pct") or 0)

    if idx >= len(candidates) or confidence < 0.62 or ev_pct < 0.04:
        return []

    game      = candidates[idx]
    team      = result.get("team", game["home_team"])
    true_prob = float(result.get("true_prob") or confidence)
    is_home   = team.lower() in game["home_team"].lower()
    yes_prob  = game["home_prob"] if is_home else game["away_prob"]
    no_prob   = round(1 - yes_prob, 4)

    question = result.get("question") or f"Will {team} win tonight?"

    return [{
        "title":      game["title"],
        "team":       team,
        "question":   question,
        "sport_key":  game["sport_key"],
        "yes_price":  yes_prob,
        "no_price":   no_prob,
        "true_prob":  true_prob,
        "side":       "yes",
        "confidence": confidence,
        "ev_pct":     ev_pct,
        "reasoning":  result.get("reasoning", ""),
        "home_odds":  game["home_odds"],
        "away_odds":  game["away_odds"],
        "commence":   game.get("commence", ""),
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

    entry_hash = hashlib.md5(
        json.dumps([p["title"] + p.get("team", "") for p in picks]).encode()
    ).hexdigest()
    hash_key = f"{_ENTRY_HASH_KEY}:{period}"
    if r.get(hash_key) == entry_hash:
        logger.info("Prediction market %s entry unchanged — skipping post", period)
        return
    r.setex(hash_key, 7200, entry_hash)

    import zoneinfo
    from datetime import datetime
    ET           = zoneinfo.ZoneInfo("America/New_York")
    now_et       = datetime.now(ET)
    date_str     = now_et.strftime("%b %-d, %Y")
    time_str     = now_et.strftime("%-I:%M %p ET")
    period_emoji = "☀️" if period == "day" else "🌙"
    period_label = "DAY" if period == "day" else "NIGHT"
    ticket_id    = hashlib.md5(f"pred{period}{date_str}".encode()).hexdigest()[:8].upper()

    pick      = picks[0]
    question  = pick.get("question", f"Will {pick.get('team', '')} win tonight?")
    sport     = (pick.get("sport_key") or "").split("_")[-1].upper() or "SPORTS"
    yes_pct   = round(pick["yes_price"] * 100)
    no_pct    = round(pick["no_price"]  * 100)
    conf      = round((pick.get("confidence") or 0) * 100)
    ev        = f"+{round((pick.get('ev_pct') or 0) * 100, 1)}%"
    cost      = round(pick["yes_price"] * 10, 2)
    reasoning = pick.get("reasoning", "")

    try:
        from dateutil.parser import parse as _p
        import zoneinfo as _zi
        _ET = _zi.ZoneInfo("America/New_York")
        game_time = _p(pick["commence"]).astimezone(_ET).strftime("%-I:%M %p ET") if pick.get("commence") else ""
    except Exception:
        game_time = ""

    embed = {
        "title": f"🔵  KALSHI SLIP  ·  {period_emoji} {period_label}",
        "description": (
            f"```\n"
            f"  Ticket #{ticket_id}          {date_str}\n"
            f"  {time_str}\n"
            f"```"
        ),
        "fields": [
            {
                "name":   "❓  QUESTION",
                "value":  f"**{question}**",
                "inline": False,
            },
            {
                "name":   "✅  ANSWER",
                "value":  f"**YES**  ·  {yes_pct}% chance",
                "inline": True,
            },
            {
                "name":   "❌  OTHER SIDE",
                "value":  f"**NO**  ·  {no_pct}% chance",
                "inline": True,
            },
            {
                "name":   "💰  COST / PAYOUT",
                "value":  f"**${cost}** → $10  ·  Edge **{ev}**  ·  Conf **{conf}%**",
                "inline": False,
            },
            {
                "name":   "🧠  REASONING",
                "value":  reasoning[:300] if reasoning else "—",
                "inline": False,
            },
            {
                "name":   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "value":  f"🔵 Kalshi  ·  Place manually  ·  {game_time or sport}",
                "inline": False,
            },
        ],
        "color": 0x1565C0,
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
        picks = _build_entry([], [], max_picks=1)
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
    """No-op: live price scan disabled — Kalshi slips use Odds API data."""
    return {"skipped": "not_applicable"}
