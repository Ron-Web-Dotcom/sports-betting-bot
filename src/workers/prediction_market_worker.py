"""
Prediction Market Worker — Kalshi entry generator.

Mirrors the HardRock entry workflow but for prediction markets:
  - Scans Kalshi for ALL live/upcoming sports markets
  - Posts a clean Discord entry: game, YES/NO odds, recommendation
  - Runs at same times as HardRock entries (day: 10:30 AM, night: 4:30 PM ET)
  - Also polls every 3 min for in-game price moves on active entries

Two entries in Discord every day:
  1. HardRock entry  — standard sportsbook (ML/spread/total)
  2. Kalshi entry    — prediction markets
"""
import json
import logging
from datetime import UTC

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


# ── Build the entry ────────────────────────────────────────────────────────────

def _fetch_todays_games() -> list[dict]:
    """
    Pull today's games: Sofascore confirms which games are TODAY,
    Odds API snapshots provide the moneyline odds for those games only.
    """
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.core.config import REDIS_URL
        import redis as _redis
        import json as _json2

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


def _build_entry(kalshi_markets: list[dict], max_picks: int = 1, period: str = "day") -> list[dict]:
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
        import redis as _rc
        import json as _jc
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
    from datetime import datetime as _dt
    _ET      = _zi.ZoneInfo("America/New_York")
    _now_et  = _dt.now(_ET)

    # Load Sofascore today's games — source of truth for what's actually playing today
    _sf_games: list[dict] = []
    try:
        import redis as _rc
        import json as _jc
        from src.core.config import REDIS_URL
        _rr = _rc.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        def _load_sf_games() -> list[dict]:
            _idx_raw = _rr.get("sofascore:today_index")
            _games: list[dict] = []
            if _idx_raw:
                _idx = _jc.loads(_idx_raw)
                if _idx:
                    _seen: set[str] = set()
                    for _ev in _idx.values():
                        _eid = _ev.get("id", "")
                        if _eid not in _seen:
                            _seen.add(_eid)
                            _games.append(_ev)
            if not _games:
                for _sfkey in ("sofascore:day_games", "sofascore:night_games"):
                    _sfraw = _rr.get(_sfkey)
                    if _sfraw:
                        _games.extend(_jc.loads(_sfraw))
            return _games

        _sf_games = _load_sf_games()

        # If cache is empty (Sofascore returned 0 games at 8 AM scan), force a fresh scan
        if not _sf_games:
            logger.info("Sofascore cache empty before %s entry — triggering rescan", period)
            try:
                from src.workers.picks_worker import scan_todays_games as _stg2
                _stg2()
                _sf_games = _load_sf_games()
            except Exception as _se2:
                logger.warning("Sofascore rescan failed: %s", _se2)

        logger.info("Kalshi _build_entry [%s]: %d Sofascore games, %d Kalshi markets",
                    period, len(_sf_games), len(kalshi_full))
    except Exception:
        pass

    def _match_sofascore(subtitle: str) -> dict | None:
        """Find the Sofascore game matching a Kalshi subtitle.
        Matches on team name OR country (for club tournaments like FIFA CWC where
        Kalshi shows country names but Sofascore has actual club names).
        """
        if not subtitle or not _sf_games:
            return None
        sl = subtitle.lower()
        best, best_score = None, 0
        for g in _sf_games:
            home         = (g.get("home_team")    or "").lower()
            away         = (g.get("away_team")    or "").lower()
            home_country = (g.get("home_country") or "").lower()
            away_country = (g.get("away_country") or "").lower()
            if not home or not away:
                continue
            # Match on team name OR country name
            # Require full team name match OR multi-word partial; single-word names (≤4 chars
            # or common English words) must match the full name to avoid false positives.
            def _team_in_title(name: str, title: str) -> bool:
                if name in title:
                    return True
                words = name.split()
                # Only do word-level partial match for names with 2+ meaningful words
                return len(words) >= 2 and any(
                    w in title for w in words if len(w) > 4
                )
            home_match = _team_in_title(home, sl) or \
                         bool(home_country and _team_in_title(home_country, sl))
            away_match = _team_in_title(away, sl) or \
                         bool(away_country and _team_in_title(away_country, sl))
            score = sum([home_match, away_match])
            if score > best_score:
                best_score, best = score, g
        return best if best_score >= 1 else None

    # Night entry: exclude any game/event already used in the day entry
    _blocked_subtitles: set[str] = set()
    _blocked_tickers:   set[str] = set()
    if period == "night":
        try:
            import json as _jb
            from src.core.config import REDIS_URL as _RURL2
            import redis as _rb
            from src.core.timezone import et_naive as _et_n
            _rb2   = _rb.from_url(_RURL2, decode_responses=True, socket_connect_timeout=2)
            _today_b = _et_n().strftime("%Y-%m-%d")
            _day_raw = _rb2.hget("slips:active", f"day:kalshi:{_today_b}")
            if _day_raw:
                _day_slip = _jb.loads(_day_raw)
                for _dp_pick in _day_slip.get("picks", []):
                    _sub = (_dp_pick.get("subtitle") or "").lower().strip()
                    _tkr = (_dp_pick.get("event_ticker") or "").upper()
                    if _sub:
                        _blocked_subtitles.add(_sub)
                    if _tkr:
                        _blocked_tickers.add(_tkr)
            logger.info("Night entry: blocking %d subtitle(s) from day slip", len(_blocked_subtitles))
        except Exception:
            pass

    candidates: list[dict] = []
    if kalshi_full:
        from dateutil.parser import parse as _dp
        from datetime import datetime as _dt2
        from zoneinfo import ZoneInfo as _ZI3
        _now_utc = _dt2.now(UTC)

        def _to_naive_et(raw: str) -> str:
            if not raw:
                return ""
            try:
                _ct = _dp(raw)
                if _ct.tzinfo is None:
                    _ct = _ct.replace(tzinfo=_ZI3("America/New_York"))
                return _ct.astimezone(_ZI3("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                return raw

        for m in kalshi_full[:200]:
            yes_prob = m.get("yes_price") or 0
            if not yes_prob or yes_prob < 0.15 or yes_prob > 0.97:
                continue
            no_prob  = round(1 - yes_prob, 4)

            subtitle = m.get("subtitle", "")

            # Night entry: skip any game already used in the day slip
            if period == "night" and (_blocked_subtitles or _blocked_tickers):
                _msub = subtitle.lower().strip()
                _mtkr = (m.get("event_ticker") or "").upper()
                if _msub and _msub in _blocked_subtitles:
                    continue
                if _mtkr and any(_mtkr.startswith(bt) or bt.startswith(_mtkr) for bt in _blocked_tickers):
                    continue

            # ── Sofascore: source of truth for game timing ─────────────────
            sf_game = _match_sofascore(subtitle)

            sport_key  = sf_game.get("sport",         "") if sf_game else ""
            sf_kickoff = sf_game.get("commence_time", "") if sf_game else ""

            # Gate on Kalshi close_time (when betting closes = game start).
            # Skip if market already closed — game is live, Kalshi stopped accepting bets.
            _close_raw = m.get("close_time") or m.get("expiration_time") or ""
            if _close_raw:
                try:
                    _close_dt = _dp(_close_raw)
                    if _close_dt.tzinfo is None:
                        _close_dt = _close_dt.replace(tzinfo=_ZI3("America/New_York"))
                    if _close_dt.astimezone(UTC) < _now_utc:
                        continue  # market closed — game already live
                except Exception:
                    pass

            if sf_kickoff:
                # Sofascore is source of truth — skip if game already live or finished
                sf_status = (sf_game.get("status_type") or sf_game.get("status") or "") if sf_game else ""
                if sf_status in ("inprogress", "finished", "canceled", "postponed"):
                    logger.debug("Kalshi: skipping '%s' — Sofascore status=%s", subtitle, sf_status)
                    continue
            else:
                if _sf_games:
                    # Sofascore loaded but no match — game not confirmed, skip
                    logger.debug("Kalshi: no Sofascore match for '%s' — skipping", subtitle)
                    continue
                # Sofascore unavailable — fall back to close_time gate (already done above)
                if not _close_raw:
                    continue  # no timing info at all — skip
                _kickoff_et = _to_naive_et(_close_raw)
                # Build candidate without Sofascore enrichment
                candidates.append({
                    "source":        "kalshi_only",
                    "title":         m.get("title", ""),
                    "event_title":   subtitle or m.get("title", ""),
                    "subtitle":      subtitle,
                    "event_ticker":  m.get("event_ticker", ""),
                    "market_id":     m.get("market_id", ""),
                    "home_team":     "",
                    "away_team":     "",
                    "sport_key":     "",
                    "sofascore_id":  "",
                    "sf_home_odds":  "",
                    "sf_draw_odds":  "",
                    "sf_away_odds":  "",
                    "sf_home_impl":  0,
                    "sf_away_impl":  0,
                    "yes_prob":      yes_prob,
                    "no_prob":       no_prob,
                    "yes_american":  m.get("yes_american", 0),
                    "no_american":   m.get("no_american", 0),
                    "volume":        m.get("volume", 0),
                    "commence_time": _kickoff_et,
                    "expiration_time": m.get("expiration_time", ""),
                })
                continue

            # commence_time = Sofascore kickoff (only path that reaches here)
            _kickoff_et = _to_naive_et(sf_kickoff)

            # Sofascore odds + enriched context for AI
            _sf_odds: dict = {}
            _sf_ctx: dict = {}
            _sf_id = sf_game.get("id", "") if sf_game else ""
            if _sf_id:
                try:
                    from src.apis.sofascore import (
                        get_event_odds as _get_sf_odds,
                        get_team_standings_for_event as _get_standings,
                        get_h2h as _get_h2h,
                        get_team_form as _get_sf_form,
                    )
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    with _TPE(max_workers=4) as _pool:
                        _fo = _pool.submit(_get_sf_odds, _sf_id)
                        _fs = _pool.submit(_get_standings, sf_game)
                        _fh = _pool.submit(_get_h2h, _sf_id)
                        _ff = _pool.submit(_get_sf_form, _sf_id)
                    _sf_odds = _fo.result() or {}
                    _sf_ctx["standings"]  = _fs.result() or {}
                    _sf_ctx["h2h"]        = _fh.result() or []
                    _sf_ctx["form"]       = _ff.result() or {}
                except Exception:
                    pass

            # Line movement from our DB — sharp/steam moves on this game
            _line_move: dict = {}
            if sf_game:
                try:
                    from src.apis.data_hub import _fetch_sharp_action as _fsa
                    _line_move = _fsa(
                        sport_key,
                        sf_game.get("home_team", ""),
                        sf_game.get("away_team", ""),
                    ) or {}
                except Exception:
                    pass

            candidates.append({
                "source":        "kalshi",
                "market_id":     m.get("market_id", ""),
                "title":         m.get("title", ""),
                "subtitle":      subtitle,
                "event_title":   subtitle or m.get("event_title", m.get("title", "")),
                "event_ticker":  m.get("event_ticker", ""),
                "sport_key":     sport_key,
                "home_team":     sf_game.get("home_team", "") if sf_game else "",
                "away_team":     sf_game.get("away_team", "") if sf_game else "",
                "tournament":    sf_game.get("tournament", "") if sf_game else "",
                "sofascore_id":  _sf_id,
                "sf_home_odds":  _sf_odds.get("home_odds", ""),
                "sf_draw_odds":  _sf_odds.get("draw_odds", ""),
                "sf_away_odds":  _sf_odds.get("away_odds", ""),
                "sf_home_impl":  _sf_odds.get("home_implied", 0),
                "sf_away_impl":  _sf_odds.get("away_implied", 0),
                "sf_standings":  _sf_ctx.get("standings", {}),
                "sf_h2h":        _sf_ctx.get("h2h", [])[:5],
                "sf_form":       _sf_ctx.get("form", {}),
                "line_movement": _line_move or None,
                "yes_prob":      yes_prob,
                "no_prob":       no_prob,
                "yes_american":  m.get("yes_american", 0),
                "no_american":   m.get("no_american", 0),
                "volume":        m.get("volume", 0),
                "commence_time": _kickoff_et,
                "expiration_time": m.get("expiration_time", ""),
            })
        candidates.sort(key=lambda x: x["volume"], reverse=True)
    logger.info("Kalshi [%s]: %d candidates from %d markets (%d sf_games)",
                period, len(candidates), len(kalshi_full), len(_sf_games))
    if not candidates and kalshi_full:

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

Each candidate includes live data from Sofascore — USE ALL OF IT:
- "market": the YES/NO question
- "home_team" / "away_team": exact team names from Sofascore — these are ground truth
- "tournament": the competition name (e.g. "FIFA Club World Cup", "NBA", "MLB")
- "game": matchup subtitle from Kalshi
- "ticker": Kalshi event ticker (KXWCGAME=FIFA CWC, KXMLBGAME=MLB, KXWNBAGAME=WNBA, KXNBAGAME=NBA, KXNFLGAME=NFL)
- "game_time_et": kickoff time
- "kalshi_yes_%": Kalshi's current market price for YES
- "sf_home_odds" / "sf_away_odds": real sportsbook odds (American) — ANCHOR for true probability
- "sf_home_implied%" / "sf_away_implied%": sportsbook implied win probability
- "sf_standings": live league/tournament table — position, points, W-D-L, GF-GA for EACH team
- "sf_form": last 5 results for each team e.g. {"home": "WWLDW", "away": "LLWDL"}
- "sf_h2h": last 5 head-to-head meetings with scores
- "line_movement": sharp money signals from sportsbook line tracking — steam_detected=True means rapid large move, sharp_moves > public_moves = smart money aligned, score > 0.6 = bullish signal

MANDATORY CONTEXT RULES — apply these BEFORE picking:

1. TEAM QUALITY: Always consider the objective quality gap between teams.
   France, Brazil, England, Germany, Spain, Argentina = top-tier national teams.
   Paraguay, Saudi Arabia, Jordan, Algeria = significantly weaker. A 90% YES on Paraguay to beat France
   is WRONG regardless of recent form — team quality trumps short-term trends.

2. STANDINGS = TRUTH: sf_standings shows where teams actually sit in the table right now.
   Position 1 vs Position 12 = massive quality gap. Use this to validate or reject Kalshi's price.

3. FORM MATTERS BUT CONTEXT MATTERS MORE: "WWWWW" against weak opponents ≠ "WWWWW" against
   top-tier competition. Weight form by opponent quality. Recent H2H results are especially useful.

4. EDGE DETECTION: Compare sf_home_implied% to kalshi_yes_% to find genuine mispricing.
   Gap of 8%+ = strong edge. Gap of 3-7% = moderate edge. Gap < 3% = weak or no edge.

5. HIGH VOLUME = SHARP MONEY: Kalshi markets with high volume have been stress-tested by smart bettors.
   Do not fight the sharp money without a specific data-backed reason.

6. SPORT-SPECIFIC RULES:
   - Soccer: goals-based markets; draw is always a real outcome
   - Basketball: higher scoring = tighter win margins; home court matters less in playoffs
   - Baseball: starting pitcher is the single biggest variable — check sf_form for recent starts
   - Tennis/MMA: head-to-head record is weighted heavily; recent form matters most

Pick the SINGLE best contract where YES has the highest REAL confidence edge.

Return ONLY valid JSON:
{
  "index": <int — index into the candidates list>,
  "answer": "YES"|"NO",
  "question": "<the market title rewritten as a clear question if needed>",
  "true_prob": <float 0.0-1.0 — your true probability of YES>,
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float e.g. 0.06 = 6% edge>,
  "reasoning": "<4-5 sentences: name both teams, cite standings position, form, H2H, and the specific Kalshi mispricing>"
}

HEAVY FAVORITE RULE: When Kalshi YES price is 85%+ AND Sofascore implied confirms the favorite,
do NOT overthink it — take the obvious lock. 85-90% YES → confidence 0.85-0.90. 90-97% → 0.90-0.97.
But VERIFY the teams are actually the implied favorite — do not apply this rule blindly.

Only pick if confidence >= 0.77 and ev_pct >= 0.005. Return {"index": null} if nothing qualifies."""

    from datetime import datetime
    import zoneinfo
    today_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%A %B %-d, %Y")

    import zoneinfo as _zii
    _ET2 = _zii.ZoneInfo("America/New_York")
    def _fmt_gt(gt: str) -> str:
        if not gt:
            return ""
        try:
            from dateutil.parser import parse as _dp2
            _d = _dp2(gt)
            if _d.tzinfo is None:
                _d = _d.replace(tzinfo=_ET2)  # naive = ET
            return _d.astimezone(_ET2).strftime("%-I:%M %p ET")
        except Exception:
            return gt[:16]

    candidate_list = [
        {k: v for k, v in {
            "index":            i,
            "market":           c.get("title", ""),
            "game":             c.get("subtitle") or c.get("event_title", ""),
            "home_team":        c.get("home_team") or None,
            "away_team":        c.get("away_team") or None,
            "tournament":       c.get("tournament") or None,
            "ticker":           c.get("event_ticker", ""),
            "game_time_et":     _fmt_gt(c.get("commence_time", "")),
            "kalshi_yes_%":     f"{round(c['yes_prob']*100)}%",
            "kalshi_yes_odds":  f"{int(c['yes_american']):+d}" if c.get("yes_american") is not None else "—",
            "volume":           c.get("volume", 0),
            # Line movement — sharp/steam signal from our sportsbook DB
            "line_movement":    c.get("line_movement") or None,
            # Sofascore bookmaker odds — use to spot Kalshi mispricing
            "sf_home_odds":     c.get("sf_home_odds") or None,
            "sf_draw_odds":     c.get("sf_draw_odds") or None,
            "sf_away_odds":     c.get("sf_away_odds") or None,
            "sf_home_implied%": f"{round(c['sf_home_impl']*100)}%" if c.get("sf_home_impl") else None,
            "sf_away_implied%": f"{round(c['sf_away_impl']*100)}%" if c.get("sf_away_impl") else None,
            # Standings — league/tournament position (positions are real: 1 = top of table)
            "sf_standings":     c.get("sf_standings") if c.get("sf_standings") else None,
            # Recent form — last 5 results e.g. {"home": "WWLDW", "away": "DLWWW"}
            "sf_form":          c.get("sf_form") if c.get("sf_form") else None,
            # H2H — last 5 head-to-head meetings
            "sf_h2h":           c.get("sf_h2h") if c.get("sf_h2h") else None,
        }.items() if v is not None}
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

    from src.workers.picks_worker import EV_FLOOR as _EV_FLOOR, CONF_FLOOR as _CONF_FLOOR

    idx        = int(result.get("index", 0))
    confidence = float(result.get("confidence") or 0)
    ev_pct     = float(result.get("ev_pct") or 0)
    answer     = result.get("answer", "YES").upper()

    # ── Perplexity last resort ─────────────────────────────────────────────────
    # Fires when AI picked a candidate but it's just under the floors (0.70-0.77 conf or 0.02-0.03 ev).
    # Web search injects fresh game intel → AI re-scores with real-world context.
    # Rate-limited to 2 calls/day shared with HardRock.
    # Only trigger Perplexity when pick is actually below floors — never overwrite qualifying picks
    _near_miss = (
        idx >= 0
        and idx < len(candidates)
        and (confidence < _CONF_FLOOR or ev_pct < _EV_FLOOR)
        and confidence >= 0.70  # reasonably close to threshold
    )
    if _near_miss:
        _perplexity_allowed = False
        try:
            from src.core.config import REDIS_URL, PERPLEXITY_API_KEY
            import redis as _rc2
            from src.core.timezone import et_naive as _et_naive2
            if PERPLEXITY_API_KEY:
                _r2 = _rc2.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
                _date_key2 = f"perplexity:calls:{_et_naive2().strftime('%Y-%m-%d')}"
                if int(_r2.get(_date_key2) or 0) < 2:
                    _perplexity_allowed = True
        except Exception:
            pass

        if _perplexity_allowed:
            try:
                from src.apis.websearch import search_game_news, search_player_news
                _pick_c   = candidates[idx]
                _subtitle = _pick_c.get("subtitle", "") or _pick_c.get("event_title", "")
                _title    = _pick_c.get("title", "")
                _sport    = _pick_c.get("sport_key", "")
                _home     = _pick_c.get("home_team", "")
                _away     = _pick_c.get("away_team", "")

                # Choose search type: player prop vs game market
                _is_player_prop = any(kw in _title.lower() for kw in
                    ("points", "rebounds", "assists", "hits", "strikeouts", "goals", "saves",
                     "yards", "touchdowns", "sets", "aces", "birdies"))
                if _is_player_prop:
                    _web = search_player_news(_subtitle or _title, _title, _sport)
                else:
                    _web = search_game_news(_home or _subtitle, _away or "", _sport, today_str)

                if _web:
                    logger.info("Kalshi Perplexity last resort: web search for '%s'", _subtitle or _title)

                    # Re-score with web context injected into the prompt
                    _enriched_prompt = (
                        f"Today is {today_str}. Fresh web intel on this Kalshi contract:\n\n"
                        f"**Market:** {_title}\n"
                        f"**Game:** {_subtitle}\n"
                        f"**Current YES price:** {round(_pick_c['yes_prob'] * 100)}%\n\n"
                        f"**Web research findings:**\n{_web}\n\n"
                        f"Re-evaluate this single contract with the above findings. "
                        f"Return the same JSON format as before."
                    )
                    _result2 = _call_json(_enriched_prompt, system)
                    if _result2 and _result2.get("index") is not None:
                        _c2 = float(_result2.get("confidence") or 0)
                        _e2 = float(_result2.get("ev_pct") or 0)
                        if _c2 >= _CONF_FLOOR and _e2 >= _EV_FLOOR:
                            result     = _result2
                            result["index"] = idx   # keep same candidate
                            confidence = _c2
                            ev_pct     = _e2
                            answer     = (_result2.get("answer") or answer).upper()
                            logger.info("Kalshi Perplexity: qualified at conf=%.0f%% ev=%.1f%%",
                                        _c2 * 100, _e2 * 100)
                            try:
                                _r2.incr(_date_key2)
                                _r2.expire(_date_key2, 86400)
                            except Exception:
                                pass
                        else:
                            logger.info("Kalshi Perplexity: still below floor (conf=%.0f%% ev=%.1f%%) — skipping",
                                        _c2 * 100, _e2 * 100)
            except Exception as _pe:
                logger.warning("Kalshi Perplexity last resort failed: %s", _pe)
    # ── End last resort ────────────────────────────────────────────────────────

    if idx < 0 or idx >= len(candidates) or confidence < _CONF_FLOOR or ev_pct < _EV_FLOOR:
        return []

    pick      = candidates[idx]

    # Hard gate: never post a pick for a game that already started > 30 min ago
    _ct = pick.get("commence_time")
    if _ct:
        try:
            from dateutil.parser import parse as _dpp
            _ct_dt = _dpp(str(_ct)) if not hasattr(_ct, "tzinfo") else _ct
            if hasattr(_ct_dt, "tzinfo") and _ct_dt.tzinfo is None:
                _ct_dt = _ct_dt.replace(tzinfo=_ZI3("America/New_York"))
            from datetime import timedelta as _tdg
            if _ct_dt.astimezone(UTC) < _now_utc - _tdg(minutes=30):
                logger.warning("Kalshi pick skipped — game started >30 min ago: %s at %s", pick.get("title"), _ct)
                return []
        except Exception:
            pass
    # Gate: EV ≥ 3%, reasoning ≥ 80 chars, ≥ 2 key factors (mirrors pick_gate thresholds)
    _reasoning = (result.get("reasoning") or "").strip()
    _factors   = [f for f in (result.get("key_factors") or []) if f and str(f).strip()]
    if ev_pct < _EV_FLOOR:  # Kalshi is a single exchange — 0.5% EV floor, confidence does the heavy lifting
        logger.info("Kalshi GATE BLOCK EV: ev=%.2f%% < %.1f%%", ev_pct * 100, _EV_FLOOR * 100)
        return []
    if len(_reasoning) < 80:
        logger.info("Kalshi GATE BLOCK REASONING: %d chars < 80", len(_reasoning))
        return []
    # key_factors is not in the Kalshi AI response schema — do not gate on it

    true_prob = float(result.get("true_prob") or confidence)
    yes_prob  = pick["yes_prob"]
    no_prob   = pick["no_prob"]
    question  = result.get("question") or pick.get("title", "") or ""

    # Derive team/subject name from title for display
    if "Will" in question:
        team = pick.get("home_team", "") or question.split("Will ")[-1].split(" win")[0]
    else:
        team = pick.get("home_team", "") or question[:40]

    return [{
        "title":        pick.get("event_title", pick.get("title", question)),
        "subtitle":     pick.get("subtitle", ""),
        "event_ticker": pick.get("event_ticker", ""),
        "team":         team,
        "question":     question,
        "answer":       answer,
        "sport_key":    pick.get("sport_key", ""),
        "market_id":    pick.get("market_id", ""),
        "home_team":    pick.get("home_team", ""),
        "away_team":    pick.get("away_team", ""),
        "yes_price":    yes_prob,
        "no_price":     no_prob,
        "true_prob":    true_prob,
        "side":         answer.lower(),
        "confidence":   confidence,
        "ev_pct":       ev_pct,
        "reasoning":    result.get("reasoning", ""),
        "home_odds":    pick.get("yes_american", 0),
        "away_odds":    pick.get("no_american", 0),
        "sofascore_id":  pick.get("sofascore_id", ""),
        "commence_time": pick.get("commence_time", ""),
    }]


# ── Discord embed ──────────────────────────────────────────────────────────────

_PLATFORM_EMOJI  = {"kalshi": "🔵"}
_PLATFORM_LABEL  = {"kalshi": "Kalshi"}


def _post_prediction_entry(period: str, picks: list[dict]) -> None:
    import hashlib
    import json
    from src.discord_bot.bot import _post

    if not picks:
        return

    r = _redis()

    entry_hash = hashlib.md5(
        json.dumps([(p.get("title") or p.get("event_title") or "") + p.get("team", "") for p in picks]).encode()
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
    # Prefer subtitle (team matchup) for sport label; fall back to ticker prefix or sport_key
    _subtitle   = pick.get("subtitle", "") or pick.get("event_title", "")
    _eticker    = (pick.get("event_ticker") or pick.get("market_id") or "").upper()
    _ticker_map = {
        # Soccer
        "KXWCGAME": "FIFA CWC", "KXWCTOTAL": "FIFA CWC",
        "KXMLSGAME": "MLS",     "KXMLSTOTAL": "MLS",
        "KXNWSLGAME": "NWSL",   "KXNWSLTOTAL": "NWSL",
        "KXEPlgame": "EPL",     "KXEPLTOTAL": "EPL",
        "KXUEFAGAME": "UEFA",   "KXUEFATOTAL": "UEFA",
        # Baseball
        "KXMLBGAME": "MLB",     "KXMLBTOTAL": "MLB",
        # Basketball
        "KXNBAGAME": "NBA",     "KXNBATOTAL": "NBA",
        "KXWNBAGAME": "WNBA",   "KXWNBATOTAL": "WNBA",
        "KXNCAABGAME": "NCAAB",
        # American Football
        "KXNFLGAME": "NFL",     "KXNFLTOTAL": "NFL",
        "KXNCAAFGAME": "NCAAF",
        # Hockey
        "KXNHLGAME": "NHL",     "KXNHLTOTAL": "NHL",
        "KXPWHLGAME": "PWHL",
        # Tennis
        "KXATPGAME": "ATP",     "KXWTGAME": "WTA",
        # Golf
        "KXPGAGAME": "PGA",     "KXLPGAGAME": "LPGA",
        # MMA / Boxing
        "KXUFCGAME": "UFC",     "KXBOXGAME": "BOXING",
        # Racing
        "KXNASCARGAME": "NASCAR", "KXF1GAME": "F1",
    }
    _sport_emoji_map = {
        # Soccer
        "FIFA CWC": "⚽", "MLS": "⚽", "NWSL": "⚽", "UEFA": "⚽",
        "EPL": "⚽", "LALIGA": "⚽", "BUNDESLIGA": "⚽", "SERIEA": "⚽",
        # Baseball
        "MLB": "⚾",
        # Basketball
        "NBA": "🏀", "WNBA": "🏀", "NCAAB": "🏀",
        # American Football
        "NFL": "🏈", "NCAAF": "🏈",
        # Hockey
        "NHL": "🏒", "PWHL": "🏒",
        # Tennis
        "ATP": "🎾", "WTA": "🎾",
        # Golf
        "PGA": "⛳", "LPGA": "⛳",
        # MMA / Boxing
        "UFC": "🥊", "MMA": "🥊", "BOXING": "🥊",
        # Racing
        "NASCAR": "🏁", "F1": "🏎️",
        # Other
        "KALSHI": "🎯",
    }
    sport = next((v for k, v in _ticker_map.items() if _eticker.startswith(k)), None) \
            or (pick.get("sport_key") or "").split("_")[-1].upper() or "KALSHI"
    sport_emoji   = _sport_emoji_map.get(sport, "🎯")
    matchup_label = _subtitle if _subtitle and _subtitle != question else sport
    answer    = (pick.get("answer") or pick.get("side") or "YES").upper()
    _yes_p    = pick.get("yes_price") or 0.5
    _no_p     = pick.get("no_price")  or round(1 - _yes_p, 2)
    yes_pct   = round(_yes_p * 100)
    no_pct    = round(_no_p  * 100)
    our_pct   = yes_pct if answer == "YES" else no_pct
    other_pct = no_pct  if answer == "YES" else yes_pct
    conf      = round((pick.get("confidence") or 0) * 100)
    _ev_val   = round((pick.get('ev_pct') or 0) * 100, 1)
    ev        = f"+{_ev_val}%" if _ev_val >= 0 else f"{_ev_val}%"
    cost      = round((_yes_p if answer == "YES" else _no_p) * 10, 2)
    reasoning = pick.get("reasoning", "")

    try:
        from dateutil.parser import parse as _p
        from zoneinfo import ZoneInfo as _ZI4
        _ET4 = _ZI4("America/New_York")
        if pick.get("commence_time"):
            _ct4 = _p(pick["commence_time"])
            if _ct4.tzinfo is None:
                _ct4 = _ct4.replace(tzinfo=_ET4)  # naive = ET, never assume UTC
            game_time = _ct4.astimezone(_ET4).strftime("%-I:%M %p ET")
        else:
            game_time = ""
    except Exception:
        game_time = ""

    embed = {
        "title": f"🔵  KALSHI SLIP  ·  {sport_emoji} {sport}  ·  {period_emoji} {period_label}",
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
                "value":  (lambda r: (next((r[:i+1] for i in range(min(280, len(r))-1, -1, -1) if r[i] in ".!?"), r[:280]) if len(r) > 280 else r))(reasoning) if reasoning else "—",
                "inline": False,
            },
            {
                "name":   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "value":  f"🔵 Kalshi  ·  {sport_emoji} {sport}" + (f"  ·  {matchup_label}" if matchup_label and matchup_label != sport else "") + (f"  ·  🕐 **{game_time}**" if game_time else ""),
                "inline": False,
            },
        ],
        "color": 0x1565C0,
    }

    try:
        from src.workers.alert_worker import _run_async
        _run_async(_post({"embeds": [embed]}))
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
    from src.discord_bot.bot import _post
    from src.workers.alert_worker import _run_async
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
        _run_async(_post({"embeds": [embed]}))
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

        # Ensure Sofascore cache is warm before building entry
        from src.core.config import REDIS_URL as _RU
        import redis as _redis_mod
        _rc = _redis_mod.from_url(_RU, decode_responses=True, socket_connect_timeout=2)
        _sf_key = f"sofascore:{period}_games"
        _sf_raw = _rc.get(_sf_key)
        if not _sf_raw or not json.loads(_sf_raw):
            logger.info("Sofascore cache cold or empty — running scan before %s entry", period)
            try:
                from src.workers.picks_worker import scan_todays_games as _stg
                _stg()
            except Exception as _se:
                logger.warning("Pre-entry Sofascore rescan failed: %s", _se)

        picks = _build_entry([], max_picks=1, period=period)
        if not picks:
            logger.info("Prediction market %s entry: no qualifying picks — staying silent", period)
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
            import hashlib as _hl
            import zoneinfo as _zi
            from datetime import datetime as _dt
            _date_str = _dt.now(_zi.ZoneInfo("America/New_York")).strftime("%b %-d, %Y")
            _ticket_id = _hl.md5(f"pred{period}{_date_str}".encode()).hexdigest()[:8].upper()
            from src.workers.slip_tracker import save_slip
            save_slip(period, "kalshi", picks, ticket_id=_ticket_id)
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
