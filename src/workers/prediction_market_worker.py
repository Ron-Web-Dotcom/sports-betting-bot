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
from zoneinfo import ZoneInfo as _ZI3

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

def _fetch_todays_games(period: str = "day") -> list[dict]:
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

            # Prefer Sofascore-confirmed games but don't block entirely — Sofascore
            # team names don't always match Odds API names exactly (e.g. "Man City" vs
            # "Manchester City"). Only skip if Sofascore is loaded AND neither team name
            # has any token overlap with any Sofascore team.
            if sofascore_teams:
                home_l, away_l = home.lower(), away.lower()
                def _any_token_match(name: str) -> bool:
                    tokens = [t for t in name.split() if len(t) > 3]
                    return name in sofascore_teams or any(
                        any(t in sf or sf in t for sf in sofascore_teams)
                        for t in tokens
                    )
                if not _any_token_match(home_l) and not _any_token_match(away_l):
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
            commence = s.get("commence_time", "")
            # Period filter — night entry: only 4 PM ET+ games.
            # Day entry: any upcoming game (no time-of-day restriction — see note in _build_entry).
            if commence:
                try:
                    from dateutil.parser import parse as _dp_g
                    import zoneinfo as _zig
                    _ct_g = _dp_g(commence)
                    if _ct_g.tzinfo is None:
                        _ct_g = _ct_g.replace(tzinfo=_zig.ZoneInfo("America/New_York"))
                    _hour_et = _ct_g.astimezone(_zig.ZoneInfo("America/New_York")).hour
                    _is_night_g = _hour_et >= 18
                    if period == "night" and not _is_night_g:
                        continue
                except Exception:
                    pass
            games[game_id] = {
                "game_id":   game_id,
                "title":     f"{away} vs {home}",
                "home_team": home,
                "away_team": away,
                "sport_key": sport,
                "commence":  commence,
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
            if kalshi_full:  # never cache empty results — a failed fetch would poison the cache for 40 min
                _r.setex("kalshi:live_markets", 2400, _jc.dumps(kalshi_full))
            else:
                logger.warning("Kalshi live API returned 0 markets — not caching empty result")
            logger.info("Kalshi markets from live API: %d sub-markets", len(kalshi_full))
    except Exception as _ke:
        logger.warning("Kalshi market fetch failed: %s", _ke)

    # Fall back to game-winner candidates from Odds API if Kalshi API empty
    games = _fetch_todays_games(period=period)
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

        # Stale cache check — if ALL cached games are from before today, treat as empty
        # (24h TTL means yesterday's scan survives until next 8 AM)
        if _sf_games:
            try:
                from dateutil.parser import parse as _dp_stale
                _today_et = _dt.now(_zi.ZoneInfo("America/New_York")).date()
                _stale = [
                    g for g in _sf_games
                    if g.get("commence_time") and _dp_stale(g["commence_time"]).date() < _today_et
                ]
                if len(_stale) == len(_sf_games):
                    logger.info("Sofascore cache is fully stale (all games from before today) — forcing rescan")
                    _sf_games = []
            except Exception:
                pass

        # If cache is empty or stale, force a fresh scan
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
    except Exception as _sfe:
        logger.warning("Kalshi _build_entry [%s]: Sofascore block failed — all markets will skip Sofascore match: %s", period, _sfe)

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
            def _team_in_title(name: str, title: str) -> bool:
                if name in title:
                    return True
                words = [w for w in name.split() if w]
                if not words:
                    return False
                # Always check the team nickname (last word: "Mets", "Cubs", "Heat", "Sox")
                # as an exact word match — handles cases where city is omitted from subtitle.
                title_words = set(title.split())
                if words[-1] in title_words:
                    return True
                # Also check any significant word (len > 4) as a substring match
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
    _blocked_teams:     set[str] = set()
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
                    # Also block by team names — subtitle formatting can differ between picks
                    for _tf in ("home_team", "away_team"):
                        _tn = (_dp_pick.get(_tf) or "").lower().strip()
                        if _tn:
                            _blocked_teams.add(_tn)
            logger.info("Night entry: blocking %d subtitle(s), %d team(s) from day slip",
                        len(_blocked_subtitles), len(_blocked_teams))
        except Exception:
            pass

    # Build event_ticker → sf_game cache for props matching.
    # Props markets (player/team props) often have subtitles like "Lionel Messi"
    # that don't contain team names. We resolve them by matching their event_ticker
    # to a game winner market we already matched (same event, different sub-market).
    _ticker_to_sf: dict[str, dict] = {}

    candidates: list[dict] = []
    if kalshi_full:
        from dateutil.parser import parse as _dp
        from datetime import datetime as _dt2
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

        # First pass: resolve event_ticker → sf_game for all game-level markets
        # so props on the same event can inherit the Sofascore match.
        for _pm in kalshi_full[:200]:
            _psub = _pm.get("subtitle", "")
            _pticker = (_pm.get("event_ticker") or "").upper()
            if _psub and _pticker and _pticker not in _ticker_to_sf:
                _pgame = _match_sofascore(_psub)
                if _pgame:
                    _ticker_to_sf[_pticker] = _pgame

        # Junk market titles — near-certain outcomes with no real edge
        _JUNK_PATTERNS = [
            "over 0.5", "will a goal be scored", "will any", "at least one",
            "will there be a", "will a run be scored", "will a point be scored",
        ]

        for m in kalshi_full[:200]:
            yes_prob = m.get("yes_price") or 0
            if not yes_prob or yes_prob < 0.15 or yes_prob > 0.97:
                continue
            no_prob  = m.get("no_price") or round(1 - yes_prob, 4)

            # Block trivially obvious markets regardless of Kalshi price
            _title_low = (m.get("title") or "").lower()
            if any(p in _title_low for p in _JUNK_PATTERNS):
                logger.info("Kalshi: skipping junk market '%s'", m.get("title", ""))
                continue

            subtitle = m.get("subtitle", "")

            # Night entry: skip any game already used in the day slip
            if period == "night" and (_blocked_subtitles or _blocked_tickers or _blocked_teams):
                _msub = subtitle.lower().strip()
                _mtkr = (m.get("event_ticker") or "").upper()
                if _msub and _msub in _blocked_subtitles:
                    continue
                if _mtkr and any(_mtkr.startswith(bt) or bt.startswith(_mtkr) for bt in _blocked_tickers):
                    continue
                # Block by team name — catches subtitle format mismatches
                if _blocked_teams and any(t in _msub for t in _blocked_teams):
                    continue

            # ── Sofascore: source of truth for game timing ─────────────────
            sf_game = _match_sofascore(subtitle)

            # Props fallback: if subtitle didn't match (e.g. "Lionel Messi"),
            # try the event_ticker cache built from game-level markets above.
            if not sf_game:
                _ev_ticker = (m.get("event_ticker") or "").upper()
                if _ev_ticker:
                    sf_game = _ticker_to_sf.get(_ev_ticker)
                    if sf_game:
                        logger.debug("Kalshi props: matched '%s' via ticker %s", subtitle, _ev_ticker)

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

            if not sf_kickoff:
                # No Sofascore match — skip. Sofascore is the only source of truth.
                logger.warning("Kalshi: skipping '%s' — no Sofascore match (have %d SF games)", subtitle, len(_sf_games))
                continue

            # Sofascore is source of truth — skip if game already live or finished
            sf_status = (sf_game.get("status_type") or sf_game.get("status") or "") if sf_game else ""
            if sf_status in ("inprogress", "finished", "canceled", "postponed"):
                logger.info("Kalshi: skipping '%s' — Sofascore status=%s", subtitle, sf_status)
                continue

            # commence_time = Sofascore kickoff (only path that reaches here)
            _kickoff_et = _to_naive_et(sf_kickoff)

            # DATE GATE: only accept games whose kickoff is TODAY (ET).
            # Tomorrow's games are "notstarted" and pass the status check — must filter by date.
            try:
                _kdt_et = _dp(_kickoff_et)
                _today_et_date = _dt2.now(_ZI3("America/New_York")).date()
                if _kdt_et.date() != _today_et_date:
                    logger.info(
                        "Kalshi: skipping '%s' — game is on %s, not today (%s)",
                        subtitle, _kdt_et.date(), _today_et_date,
                    )
                    continue  # tomorrow's game — never pick it today
            except Exception:
                pass  # can't parse date — allow through rather than block

            # Period gate:
            #   Night entry: only games starting 4 PM ET+ (true evening games).
            #   Day entry:   any upcoming game — no time restriction.
            #     Rationale: in summer (July), most games start at 7 PM ET. Restricting
            #     the day entry to before-4PM games means 0 candidates almost every day.
            #     The night entry already blocks the day pick via _blocked_subtitles.
            try:
                _kdt_et = _dp(_kickoff_et)
                _is_night_game = _kdt_et.hour >= 18
                if period == "night" and not _is_night_game:
                    continue  # day game in night entry — skip
                if period == "day" and _is_night_game:
                    continue  # night game in day entry — skip
            except Exception:
                pass  # can't parse — allow through

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
                        get_event_lineups as _get_lineups,
                        get_featured_players as _get_players,
                        get_match_trends as _get_trends,
                    )
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    with _TPE(max_workers=7) as _pool:
                        _fo  = _pool.submit(_get_sf_odds, _sf_id)
                        _fs  = _pool.submit(_get_standings, sf_game)
                        _fh  = _pool.submit(_get_h2h, _sf_id)
                        _ff  = _pool.submit(_get_sf_form, _sf_id)
                        _fl  = _pool.submit(_get_lineups, _sf_id)
                        _fp  = _pool.submit(_get_players, _sf_id)
                        _ft  = _pool.submit(_get_trends, _sf_id)
                    _sf_odds = _fo.result() or {}
                    _sf_ctx["standings"]         = _fs.result() or {}
                    _sf_ctx["h2h"]               = _fh.result() or []
                    _sf_ctx["form"]              = _ff.result() or {}
                    _sf_ctx["lineups"]           = _fl.result() or {}
                    _sf_ctx["featured_players"]  = _fp.result() or {}
                    _sf_ctx["match_trends"]      = _ft.result() or {}
                except Exception:
                    pass

            # Build rich odds summary for AI — include totals, BTTS, handicap
            _sf_totals    = _sf_odds.get("totals",    [])
            _sf_btts      = {k: v for k, v in _sf_odds.items() if k.startswith("btts_")}
            _sf_handicaps = _sf_odds.get("handicaps", [])

            # ── Player profile — only for player prop markets ──────────────
            # Detect player prop: subtitle is a single name (not "Team A vs Team B")
            # e.g. "Lionel Messi", "Shohei Ohtani", "Patrick Mahomes"
            _player_profile: dict = {}
            _player_season_stats: dict = {}
            _title_lower = m.get("title", "").lower()
            _is_player_prop = (
                subtitle
                and " vs " not in subtitle.lower()
                and " @ " not in subtitle.lower()
                and len(subtitle.split()) <= 4   # names are short, team matchups are longer
                and any(kw in _title_lower for kw in (
                    "score", "goal", "assist", "hit", "home run", "strikeout",
                    "point", "rebound", "assist", "block", "steal", "three",
                    "passing yard", "rushing yard", "receiving yard", "touchdown",
                    "saves", "shot", "ace", "birdie", "ko", "tko", "submission",
                    "win by", "record", "player prop",
                ))
            )
            if _is_player_prop:
                try:
                    from src.apis.sofascore import (
                        search_player as _search_player,
                        get_player_profile as _get_pprofile,
                        get_player_season_stats as _get_pstats,
                    )
                    _search_results = _search_player(subtitle, sport_key)
                    if _search_results:
                        _pid = _search_results[0].get("id", "")
                        if _pid:
                            # Sequential — only 2 calls, no parallel to keep rate low
                            _player_profile = _get_pprofile(_pid) or {}
                            if _player_profile.get("team_id"):
                                _player_season_stats = _get_pstats(_pid) or {}
                except Exception as _pe:
                    logger.debug("Player profile fetch failed for '%s': %s", subtitle, _pe)

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
                "sf_home_odds":   _sf_odds.get("home_odds", ""),
                "sf_draw_odds":   _sf_odds.get("draw_odds", ""),
                "sf_away_odds":   _sf_odds.get("away_odds", ""),
                "sf_home_impl":   _sf_odds.get("home_implied", 0),
                "sf_draw_impl":   _sf_odds.get("draw_implied", 0),
                "sf_away_impl":   _sf_odds.get("away_implied", 0),
                "sf_totals":          _sf_totals[:3],
                "sf_btts":            _sf_btts or {},
                "sf_handicaps":       _sf_handicaps[:2],
                "sf_standings":       _sf_ctx.get("standings", {}),
                "sf_h2h":             _sf_ctx.get("h2h", [])[:5],
                "sf_form":            _sf_ctx.get("form", {}),
                "sf_lineups":          _sf_ctx.get("lineups", {}),
                "sf_featured_players": _sf_ctx.get("featured_players", {}),
                "sf_match_trends":     _sf_ctx.get("match_trends", {}),
                "player_profile":      _player_profile or None,
                "player_season_stats": _player_season_stats or None,
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
        logger.warning("Kalshi [%s]: 0 candidates after Sofascore filter — all %d markets had no Sofascore match",
                       period, len(kalshi_full))
    if not candidates:

        # Odds API fallback — game winners only
        _today_et_date_fb = _dt2.now(_ZI3("America/New_York")).date()
        for g in games:
            if not (0.20 <= g["home_prob"] <= 0.80):
                continue
            _cmt = g.get("commence", "")
            if _cmt:
                try:
                    _ct_fb = _dp(_cmt)
                    if _ct_fb.tzinfo is None:
                        _ct_fb = _ct_fb.replace(tzinfo=_ZI3("America/New_York"))
                    if _ct_fb.astimezone(_ZI3("America/New_York")).date() != _today_et_date_fb:
                        logger.info("Odds API fallback: skipping '%s' — game is not today", g["title"])
                        continue
                except Exception:
                    pass
            candidates.append({
                "source":        "odds_api",
                "title":         g["title"],
                "event_title":   g["title"],
                "subtitle":      g["title"],
                "yes_prob":      g["home_prob"],
                "no_prob":       round(1 - g["home_prob"], 4),
                "yes_american":  int(g["home_odds"]),
                "no_american":   int(g["away_odds"]),
                "home_team":     g["home_team"],
                "away_team":     g["away_team"],
                "sport_key":     g["sport_key"],
                "sofascore_id":  "",
                "volume":        0,
                "commence_time": g.get("commence", ""),
            })

    if not candidates:
        return []

    system = """You are an elite sports analyst and prediction market researcher specializing in Kalshi.

Each candidate includes live data from Sofascore — USE ALL OF IT:
- "market": the YES/NO question (game winners, totals, BTTS, team props, player props)
- "home_team" / "away_team": exact team names from Sofascore — these are ground truth
- "tournament": competition name (e.g. "FIFA Club World Cup", "NBA", "MLB")
- "game": matchup subtitle from Kalshi
- "ticker": Kalshi event ticker (KXWCGAME=FIFA CWC, KXMLBGAME=MLB, KXWNBAGAME=WNBA, KXNBAGAME=NBA)
- "game_time_et": kickoff time
- "kalshi_yes_%": Kalshi's current market price for YES
- "sf_home_odds" / "sf_away_odds" / "sf_draw_odds": REAL sportsbook odds (American) — ANCHOR for true probability
- "sf_home_implied%" / "sf_away_implied%" / "sf_draw_implied%": sportsbook implied win probability
- "sf_totals": Sofascore over/under lines [{line, over_odds, under_odds, over_implied, under_implied}]
  → compare to Kalshi totals markets — if Sofascore says o2.5 is -150 and Kalshi prices it at 60%, that's edge
- "sf_btts": both-teams-to-score odds {btts_yes_odds, btts_no_odds, btts_yes_implied, btts_no_implied}
  → compare to Kalshi BTTS markets
- "sf_handicaps": Asian handicap lines [{line, home_odds, away_odds}]
- "sf_standings": live league/tournament table — position, points, W-D-L, GF-GA for EACH team
- "sf_form": last 5 results for each team e.g. {"home": "WWLDW", "away": "LLWDL"}
- "sf_h2h": last 5 head-to-head meetings with scores
- "sf_lineups": starting XI + formation + player ratings from last match — KEY for team strength assessment
  → check formation (4-3-3 vs 5-4-1), key attackers (high rating = in-form), missing stars
- "sf_featured_players": Sofascore's top-rated players — ATT/TEC/CRE/TAC/DEF radar stats and last-match rating
  → Messi 9.5 = dominant form; a 6.5-rated striker = cold. Use this for player props and BTTS calls.
- "player_profile": full Sofascore profile for the specific player in a player-prop market
  → attributes {attacking, technical, tactical, defending, creativity} — 0 to 99 scale
  → last5_avg_rating, last5_goals, last5_assists — direct recent form numbers
  → recent_matches: [{date, home_team, away_team, rating, goals, assists, shots, minutes}]
  → strengths, weaknesses e.g. ["Scoring", "Ground duels", "Consistency"]
- "player_season_stats": goals, assists, rating, shots, goals_per90 for current season
- "sf_match_trends": Sofascore's pre-match statistical trends — BTTS %, clean sheet %, o/u %, fan vote
  → "both_teams_to_score: {home: '5/5', away: '5/5'}" = strong BTTS lean
  → "under_2.5_goals: {away: '5/7'}" = away team plays tight
- "line_movement": sharp money signals — steam_detected=True means rapid large move, sharp_moves > public_moves = smart money aligned

MANDATORY CONTEXT RULES — apply these BEFORE picking:

1. TEAM QUALITY: Always consider the objective quality gap between teams.
   France, Brazil, England, Germany, Spain, Argentina = top-tier national teams.
   Paraguay, Saudi Arabia, Jordan, Algeria = significantly weaker. A 90% YES on Paraguay to beat France
   is WRONG regardless of recent form — team quality trumps short-term trends.

2. STANDINGS = TRUTH: sf_standings shows where teams actually sit in the table right now.
   Position 1 vs Position 12 = massive quality gap. Use this to validate or reject Kalshi's price.

3. SOFASCORE ODDS = MARKET TRUTH: sf_totals/sf_btts/sf_handicaps are REAL bookmaker lines.
   If Kalshi prices "over 2.5 goals YES" at 55% but sf_totals shows over_implied=0.62, Kalshi is underpriced.
   If Kalshi "BTTS YES" is 60% but sf_btts shows btts_yes_implied=0.45, Kalshi is overpriced → pick NO.

4. LINEUPS = TEAM STRENGTH: sf_lineups shows actual formations and who's starting.
   A team missing its top striker/playmaker is drastically weaker for goals markets.
   Lineup data overrides general form — a star player starting at 9.5 rating = confidence boost.

5. MATCH TRENDS = BASE RATES: sf_match_trends gives recent statistical frequencies.
   "BTTS home: 5/5" = 100% — strong lean regardless of current odds.
   "under 2.5 away: 5/7" = 71% — meaningful but not absolute.
   Cross-reference: if Kalshi prices BTTS YES at 45% but trends say home BTTS 5/5 + away BTTS 5/5, that's a steal.

6. PLAYER PROPS — when player_profile is present, USE IT:
   - player_profile.attributes: ATT/TEC/CRE/TAC/DEF (0-99) — ATT 90+ = elite scorer, CRE 80+ = elite creator
   - player_profile.last5_avg_rating: 8.5+ = dominant recent form, under 7.0 = cold
   - player_profile.last5_goals: goals in last 5 matches — use as direct frequency signal
   - player_profile.recent_matches: game-by-game log — check if goals came vs strong or weak opponents
   - player_profile.strengths / weaknesses: "Scoring", "Dribbling" etc. — validates scorer prop
   - player_season_stats.goals / goals_per90: season-level scoring rate
   Decision rule: player rated 8.5+ last 5, ATT 85+, scored in 3 of last 5 → strong scorer YES.
   Kalshi player YES at 30% when player has scored in 4 of last 5 = massive value. Take it.

5. EDGE DETECTION: Compare sf_*_implied% to kalshi_yes_% to find genuine mispricing.
   Gap of 8%+ = strong edge. Gap of 3-7% = moderate edge. Gap < 3% = weak or no edge.

6. HIGH VOLUME = SHARP MONEY: Kalshi markets with high volume have been stress-tested by smart bettors.
   Do not fight the sharp money without a specific data-backed reason.

7. SPORT-SPECIFIC RULES:
   - Soccer: draw is always a real outcome; BTTS and totals are excellent markets for edge
   - Basketball: higher scoring = tighter win margins; home court matters less in playoffs
   - Baseball: starting pitcher is the single biggest variable
   - Tennis/MMA: head-to-head record is weighted heavily; recent form matters most

Pick the SINGLE best contract where YES has the highest REAL confidence edge — game winner, total, BTTS, team prop, or player prop.

Return ONLY valid JSON:
{
  "index": <int — index into the candidates list>,
  "answer": "YES"|"NO",
  "question": "<the market title rewritten as a clear question if needed>",
  "true_prob": <float 0.0-1.0 — your true probability of YES>,
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float e.g. 0.06 = 6% edge>,
  "reasoning": "<4-5 sentences: name both teams, cite standings/form/H2H, state the specific Sofascore odds anchor used, and explain the Kalshi mispricing>"
}

FAVORITES RULE (HARD): Always check the American odds equivalent of the Kalshi YES price.
- Negative odds (kalshi_yes_odds like -150, -200) = the team is a FAVORITE. Lean YES on favorites.
- Positive odds (kalshi_yes_odds like +120, +180) = the team is an UNDERDOG. Only pick YES on an underdog
  if there is a SPECIFIC data-backed reason (injury to the opponent's starter, dominant recent H2H,
  much better form). Never pick an underdog simply because the payout is higher.
- When choosing between two similar-edge candidates: ALWAYS choose the one with negative odds (the favorite).
  Favorites win more often — that's math. Underdogs are exciting but kill W/L records over time.
- sf_home_odds / sf_away_odds shows the real sportsbook line. Negative = that team is favored by the market.
  If sf_home_odds is -160 and Kalshi YES (home wins) is at 58%, that's a 4-point underpricing — take it.
  If sf_home_odds is +130 and Kalshi YES (home wins) is at 45%, the sportsbook says this is an underdog
  — be very cautious unless Sofascore data gives a strong reason to override.

HEAVY FAVORITE RULE: When Kalshi YES price is 85%+ AND Sofascore implied confirms the favorite,
do NOT overthink it — take the obvious lock. 85-90% YES → confidence 0.85-0.90. 90-97% → 0.90-0.97.
But VERIFY the teams are actually the implied favorite — do not apply this rule blindly.

JUNK MARKET BLACKLIST — NEVER pick these regardless of price or edge:
- "over 0.5 goals", "will a goal be scored", "will any team score" — trivially obvious, no real edge
- "over 0.5 runs", "will any run be scored" — same issue for baseball
- Any market where the true probability is above 95% — Kalshi prices it correctly, no edge
- Any market phrased as "will at least one X happen" where X is near-guaranteed

TRUNCATION RULE: Always write full team names. Never abbreviate — "Los Angeles Dodgers" not "Los Angeles D", "New York Yankees" not "NYY".

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
            "sf_home_odds":      c.get("sf_home_odds") or None,
            "sf_draw_odds":      c.get("sf_draw_odds") or None,
            "sf_away_odds":      c.get("sf_away_odds") or None,
            "sf_home_implied%":  f"{round(c['sf_home_impl']*100)}%" if c.get("sf_home_impl") else None,
            "sf_draw_implied%":  f"{round(c['sf_draw_impl']*100)}%" if c.get("sf_draw_impl") else None,
            "sf_away_implied%":  f"{round(c['sf_away_impl']*100)}%" if c.get("sf_away_impl") else None,
            # Sofascore totals — over/under lines with bookmaker odds
            "sf_totals":          c.get("sf_totals") if c.get("sf_totals") else None,
            # Sofascore BTTS — both teams to score odds
            "sf_btts":            c.get("sf_btts") if c.get("sf_btts") else None,
            # Sofascore handicap lines
            "sf_handicaps":       c.get("sf_handicaps") if c.get("sf_handicaps") else None,
            # Lineups — starting XI formations (key injury/suspension signal)
            "sf_lineups":         c.get("sf_lineups") if c.get("sf_lineups") else None,
            # Featured players — Sofascore's top-rated players with ATT/TEC/CRE/TAC/DEF
            "sf_featured_players": c.get("sf_featured_players") if c.get("sf_featured_players") else None,
            # Match trends — BTTS %, clean sheet %, over/under %, fan vote from Sofascore
            "sf_match_trends":    c.get("sf_match_trends") if c.get("sf_match_trends") else None,
            # Player profile — only set for player prop markets
            # Includes: name, position, club, ATT/CRE/TEC/TAC/DEF, strengths, weaknesses,
            #           last 5 matches with ratings/goals/assists, avg rating last 5
            "player_profile":     c.get("player_profile") or None,
            # Season stats — goals, assists, rating, shots, minutes for current season
            "player_season_stats": c.get("player_season_stats") or None,
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
        logger.warning(
            "Kalshi bail-out J [%s]: idx=%d candidates=%d conf=%.1f%% (floor %.1f%%) ev=%.2f%% (floor %.2f%%) — no pick",
            period, idx, len(candidates), confidence * 100, _CONF_FLOOR * 100, ev_pct * 100, _EV_FLOOR * 100,
        )
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


def _post_prediction_entry(period: str, picks: list[dict]) -> bool:
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
        "KXEPLGAME": "EPL",     "KXEPLTOTAL": "EPL",
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
    # American odds for display
    _our_odds_val   = pick.get("yes_american", 0) if answer == "YES" else pick.get("no_american", 0)
    _other_odds_val = pick.get("no_american",  0) if answer == "YES" else pick.get("yes_american", 0)
    def _fmt_am(v) -> str:
        try:
            vi = int(v or 0)
            if vi == 0:
                return ""          # missing/unknown — don't show "0"
            return f"+{vi}" if vi > 0 else str(vi)
        except Exception:
            return ""
    _our_odds   = _fmt_am(_our_odds_val)
    _other_odds = _fmt_am(_other_odds_val)

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
                "value":  f"**{answer}**  ·  {our_pct}% chance" + (f"  `{_our_odds}`" if _our_odds else ""),
                "inline": True,
            },
            {
                "name":   "❌  OTHER SIDE",
                "value":  f"{'NO' if answer == 'YES' else 'YES'}  ·  {other_pct}% chance" + (f"  `{_other_odds}`" if _other_odds else ""),
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
        _ok = _run_async(_post({"embeds": [embed]}))
        if _ok:
            logger.info("Prediction market %s entry posted successfully (%d picks)", period, len(picks))
            return True
        else:
            logger.error("Prediction market %s entry Discord post returned False — webhook may have failed", period)
            return False
    except Exception as e:
        logger.error("Failed to post prediction market entry: %s", e)
        return False


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

        _posted = _post_prediction_entry(period, picks)

        if not _posted:
            logger.error("Kalshi %s entry: Discord post failed — releasing dedup lock so next run can retry", period)
            try:
                if _r_dedup and _dedup_key:
                    _r_dedup.delete(_dedup_key)
            except Exception:
                pass
            return {"picks": len(picks), "posted": False}

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
