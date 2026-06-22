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
    """
    Pull today's games: Sofascore confirms which games are TODAY,
    Odds API snapshots provide the moneyline odds for those games only.
    """
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.core.config import REDIS_URL
        import redis as _redis, json as _json2

        # Load Sofascore's confirmed today list (populated by scan_todays_games at 8 AM + 2 PM)
        sofascore_teams: set[str] = set()
        try:
            r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
            for key in ("sofascore:day_games", "sofascore:night_games"):
                raw = r.get(key)
                if raw:
                    for ev in _json2.loads(raw):
                        sofascore_teams.add(ev.get("home_team", "").lower())
                        sofascore_teams.add(ev.get("away_team", "").lower())
        except Exception:
            pass  # if Redis is down fall through to odds-only

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

            # Only include if Sofascore confirms these teams play today
            if sofascore_teams and \
               home.lower() not in sofascore_teams and \
               away.lower() not in sofascore_teams:
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
    Score today's Kalshi markets across ALL market types:
    game winners, player props, game props (totals, BTTS, spreads, team totals).
    Returns the single best qualifying pick as a Kalshi YES/NO contract.
    """
    import json as _json
    from src.engines.ai_engine import _call_json

    # Pull full Kalshi event markets — Redis cache (refreshed every 20 min by scan_player_props)
    # Falls back to live API call if cache is cold
    kalshi_full: list[dict] = []
    try:
        from src.core.config import REDIS_URL
        import redis as _rc, json as _jc
        _r = _rc.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _cached = _r.get("kalshi:live_markets")
        if _cached:
            kalshi_full = _jc.loads(_cached)
            logger.info("Kalshi markets from cache: %d sub-markets", len(kalshi_full))
        else:
            from src.apis.kalshi import get_sports_events
            kalshi_full = get_sports_events(limit=200)
            _r.setex("kalshi:live_markets", 2400, _jc.dumps(kalshi_full))
            logger.info("Kalshi markets from live API: %d sub-markets", len(kalshi_full))
    except Exception as _ke:
        logger.warning("Kalshi market fetch failed: %s", _ke)

    # Fall back to game-winner candidates from Odds API if Kalshi API empty
    games = _fetch_todays_games()
    if not kalshi_full and not games:
        logger.info("Kalshi entry: no markets available")
        return []

    # Build candidate list — Kalshi full markets preferred, Odds API games as fallback
    import zoneinfo as _zi
    from datetime import datetime as _dt, timedelta as _td
    _ET      = _zi.ZoneInfo("America/New_York")
    _now_et  = _dt.now(_ET)
    _today   = _now_et.date()

    # Load Sofascore today index to verify teams are actually playing TODAY
    _today_teams: set[str] = set()
    try:
        import redis as _rc
        from src.core.config import REDIS_URL
        _rr = _rc.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _idx = _rr.get("sofascore:today_index")
        if _idx:
            _today_teams = set(json.loads(_idx).keys())
    except Exception:
        pass

    def _team_in_title(title: str) -> bool:
        if not _today_teams:
            return True  # can't verify — allow through
        tl = title.lower()
        return any(team in tl for team in _today_teams)

    candidates: list[dict] = []
    if kalshi_full:
        # Use Kalshi's own markets — filter to markets closing TODAY in ET
        # AND cross-reference with Sofascore to confirm game is today
        for m in kalshi_full[:100]:
            yes_prob = m.get("yes_price", 0)
            no_prob  = round(1 - yes_prob, 4)
            if not yes_prob or yes_prob < 0.15 or yes_prob > 0.97:
                continue
            # Drop markets whose close_time is not today ET
            _ct = m.get("close_time", "")
            if _ct:
                try:
                    from dateutil.parser import parse as _dp
                    _ct_et = _dp(_ct).astimezone(_ET).date()
                    if _ct_et != _today:
                        continue
                except Exception:
                    pass  # keep if unparseable
            # Drop if neither team plays today per Sofascore
            _title = m.get("title", "")
            if _title and not _team_in_title(_title):
                logger.info("Kalshi: skipping '%s' — not in today's schedule", _title[:60])
                continue
            candidates.append({
                "source":       "kalshi",
                "market_id":    m.get("market_id", ""),
                "title":        m.get("title", ""),
                "event_title":  m.get("event_title", m.get("title", "")),
                "yes_prob":     yes_prob,
                "no_prob":      no_prob,
                "yes_american": m.get("yes_american", 0),
                "no_american":  m.get("no_american", 0),
                "volume":       m.get("volume", 0),
                "close_time":   m.get("close_time", ""),
            })
        candidates.sort(key=lambda x: x["volume"], reverse=True)  # highest liquidity first
    else:
        # Odds API fallback — game winners only
        for g in games:
            if not (0.20 <= g["home_prob"] <= 0.80):
                continue
            candidates.append({
                "source":       "odds_api",
                "title":        g["title"],
                "event_title":  g["title"],
                "yes_prob":     g["home_prob"],
                "no_prob":      round(1 - g["home_prob"], 4),
                "yes_american": int(g["home_odds"]),
                "no_american":  int(g["away_odds"]),
                "home_team":    g["home_team"],
                "away_team":    g["away_team"],
                "sport_key":    g["sport_key"],
                "volume":       0,
                "close_time":   g.get("commence", ""),
            })

    if not candidates:
        return []

    system = """You are an elite sports analyst and prediction market researcher specializing in Kalshi.

You will be given a list of Kalshi YES/NO contracts covering ALL market types:
- Game winners (Will Team X win?)
- Player props (Will [Player] score over X points/goals?)
- Game props (Will total goals be over X? Will both teams score? Will Team X cover -1.5?)
- Team totals, spreads, BTTS, alternate lines

For each contract research deeply:
- Recent form, stats, injuries, matchup edges
- Whether the YES probability is mis-priced vs true probability
- High volume = sharp money — respect it

Pick the SINGLE best contract where YES has the highest confidence edge.

Return ONLY valid JSON:
{
  "index": <int — index into the candidates list>,
  "answer": "YES"|"NO",
  "question": "<the market title rewritten as a clear question if needed>",
  "true_prob": <float 0.0-1.0 — your true probability of YES>,
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float e.g. 0.06 = 6% edge>,
  "reasoning": "<3-4 sentences: what you researched, what edge you found, why the market is wrong>"
}

Only pick if confidence >= 0.60 and ev_pct >= 0.03. Return {"index": null} if nothing qualifies."""

    from datetime import datetime
    import zoneinfo
    today_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%A %B %-d, %Y")

    candidate_list = [
        {
            "index":        i,
            "market":       c.get("title", ""),
            "event":        c.get("event_title", ""),
            "yes_prob_%":   f"{round(c['yes_prob']*100)}%",
            "yes_american": f"{c['yes_american']:+d}" if c.get("yes_american") else "—",
            "volume":       c.get("volume", 0),
        }
        for i, c in enumerate(candidates[:80])
    ]

    prompt = (
        f"Today is {today_str}. These are today's available Kalshi contracts (sorted by volume):\n\n"
        f"```json\n{_json.dumps(candidate_list, indent=2)}\n```\n\n"
        f"Research each market deeply — player props, game props, game winners, totals, BTTS. "
        f"Find the single contract with the most confident edge where the market price is wrong."
    )

    try:
        result = _call_json(prompt, system)
    except Exception as e:
        logger.warning("Kalshi AI scoring failed: %s", e)
        return []

    if not result or result.get("index") is None:
        logger.info("Kalshi AI: no qualifying pick")
        return []

    idx        = int(result.get("index", 0))
    confidence = float(result.get("confidence") or 0)
    ev_pct     = float(result.get("ev_pct") or 0)
    answer     = result.get("answer", "YES").upper()

    if idx >= len(candidates) or confidence < 0.60 or ev_pct < 0.03:
        return []

    pick      = candidates[idx]
    true_prob = float(result.get("true_prob") or confidence)
    yes_prob  = pick["yes_prob"]
    no_prob   = pick["no_prob"]
    question  = result.get("question") or pick.get("title", "")

    # Derive team/subject name from title for display
    team = pick.get("home_team", "") or question.split("Will ")[-1].split(" win")[0] if "Will" in question else question[:40]

    return [{
        "title":      pick.get("event_title", pick.get("title", question)),
        "team":       team,
        "question":   question,
        "answer":     answer,
        "sport_key":  pick.get("sport_key", ""),
        "market_id":  pick.get("market_id", ""),
        "home_team":  pick.get("home_team", ""),
        "away_team":  pick.get("away_team", ""),
        "yes_price":  yes_prob,
        "no_price":   no_prob,
        "true_prob":  true_prob,
        "side":       answer.lower(),
        "confidence": confidence,
        "ev_pct":     ev_pct,
        "reasoning":  result.get("reasoning", ""),
        "home_odds":  pick.get("yes_american", 0),
        "away_odds":  pick.get("no_american", 0),
        "commence_time": pick.get("close_time", ""),
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
    answer    = (pick.get("answer") or pick.get("side") or "YES").upper()
    yes_pct   = round(pick["yes_price"] * 100)
    no_pct    = round(pick["no_price"]  * 100)
    our_pct   = yes_pct if answer == "YES" else no_pct
    other_pct = no_pct  if answer == "YES" else yes_pct
    conf      = round((pick.get("confidence") or 0) * 100)
    ev        = f"+{round((pick.get('ev_pct') or 0) * 100, 1)}%"
    cost      = round((pick["yes_price"] if answer == "YES" else pick["no_price"]) * 10, 2)
    reasoning = pick.get("reasoning", "")

    try:
        from dateutil.parser import parse as _p
        import zoneinfo as _zi
        _ET = _zi.ZoneInfo("America/New_York")
        game_time = _p(pick["commence_time"]).astimezone(_ET).strftime("%-I:%M %p ET") if pick.get("commence_time") else ""
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
                "value":  f"**{answer}**  ·  {our_pct}% chance",
                "inline": True,
            },
            {
                "name":   "❌  OTHER SIDE",
                "value":  f"{'NO' if answer == 'YES' else 'YES'}  ·  {other_pct}% chance",
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
    _r_dedup = None
    _dedup_key = None
    try:
        # Dedup: atomic SET NX so two simultaneous restarts can't both post
        # Use 1h TTL initially — extended to 24h only after successful post
        from src.core.timezone import et_naive as _et_naive
        _today = _et_naive().strftime("%Y-%m-%d")
        _dedup_key = f"kalshi:posted:{period}:{_today}"
        _r_dedup = _redis()
        if not _r_dedup.set(_dedup_key, "1", ex=3600, nx=True):
            logger.info("Kalshi %s entry already posted today — skipping", period)
            return {"skipped": "already_posted", "period": period}

        picks = _build_entry([], [], max_picks=1)
        if not picks:
            logger.info("Prediction market %s entry: no qualifying picks", period)
            try:
                from src.workers.alert_worker import _run_async
                from src.discord_bot.bot import _post
                period_emoji = "☀️" if period == "day" else "🌙"
                period_label = "DAY ENTRY" if period == "day" else "NIGHT ENTRY"
                _run_async(_post({"embeds": [{
                    "title": f"📊  KALSHI SLIP  ·  {period_emoji} {period_label}",
                    "description": (
                        "No qualifying research pick for this session.\n"
                        "Confidence below 65% or edge too thin — skipping to protect bankroll."
                    ),
                    "color": 0x546E7A,
                    "footer": {"text": "Kalshi  ·  Research-backed  ·  Bet responsibly"},
                }]}))
            except Exception as _e:
                logger.warning("Could not post no-picks Kalshi alert: %s", _e)
            # Release lock so a retry can attempt a different market
            try:
                if _r_dedup and _dedup_key:
                    _r_dedup.delete(_dedup_key)
            except Exception:
                pass
            return {"picks": 0, "posted": False}

        _post_prediction_entry(period, picks)

        # Extend to full 24h now that posting succeeded
        try:
            if _r_dedup and _dedup_key:
                _r_dedup.expire(_dedup_key, 86400)
        except Exception:
            pass

        try:
            from src.workers.slip_tracker import save_slip
            save_slip(period, "kalshi", picks)
        except Exception as e:
            logger.warning("slip_tracker.save_slip failed: %s", e)

        return {"period": period, "picks": len(picks), "posted": True}
    except Exception as exc:
        logger.error("Prediction market %s entry failed: %s", period, exc)
        # Release lock so retry is possible
        try:
            if _r_dedup and _dedup_key:
                _r_dedup.delete(_dedup_key)
        except Exception:
            pass
        return {"error": str(exc)}


def scan_prediction_markets() -> dict:
    """No-op: live price scan disabled — Kalshi slips use Odds API data."""
    return {"skipped": "not_applicable"}
