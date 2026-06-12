"""
Picks worker — full pick generation pipeline.

For each upcoming event:
  1. Fetch latest odds + injuries + news
  2. Run all engines (EV, confidence, risk, comparison, parlay)
  3. Build PickRecommendation
  4. Persist to DB
  5. Route BET picks to alert queue
"""
import logging
from src.db.session import get_db
from src.db.models import Game

logger = logging.getLogger(__name__)


def _post_parlay_bundles(pick_dicts: list[dict], hr_task) -> None:
    """Bundle top picks into a HardRock parlay card — max 4 legs, min 2."""
    hr_picks = sorted(pick_dicts, key=lambda p: p.get("confidence", 0), reverse=True)
    if len(hr_picks) >= 2:
        hr_task(hr_picks[:4])


def _is_sleep_time() -> bool:
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    # Quiet window: 11 PM – 5 AM ET — no picks posted overnight
    return et.hour >= 23 or et.hour < 5


def generate_picks():
    """
    Unified pick generation -- runs every 20 min.
    Scores games (ML/spread/total) + player props, merges into one ranked pool,
    picks top 2-5 by confidence x EV, posts a single Discord embed.
    Same-game uniqueness: if a game pick is selected, no prop from that game.
    Only posts when the selection changes since last run.
    """
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}
    try:
        from src.core.config import REDIS_URL
        import redis as _redis, json, hashlib, zoneinfo
        from datetime import datetime
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.engines.news_engine import get_recent_injuries
        from src.engines.confidence_engine import compute_confidence
        from src.engines.ev_engine import evaluate, decimal_to_american, implied_prob
        from src.engines.ai_engine import analyse_pick
        from src.apis.data_hub import build_game_context
        from src.core.sport_labels import get_emoji, get_name
        from src.workers.alert_worker import _run_async
        from src.discord_bot.bot import _post

        ET = zoneinfo.ZoneInfo("America/New_York")
        r  = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # Load Sofascore today index — used ONLY to get exact kick-off times
        # (day vs night split). We already know what's playing from 8 AM scan.
        # No re-checking, no filtering — Sofascore just tells us WHEN each game starts.
        _sf_raw = r.get("sofascore:today_index")
        sofascore_index: dict[str, dict] = json.loads(_sf_raw) if _sf_raw else {}

        def _sf_enrich(home: str, away: str, fallback_time: str) -> str:
            """Return Sofascore's exact kick-off time if available, else Odds API time."""
            for name in (home.lower(), away.lower()):
                ev = sofascore_index.get(name)
                if ev and ev.get("commence_time"):
                    return ev["commence_time"]
            return fallback_time

        # -- 1. Score game picks (ML / spread / total) --------------------
        snapshots  = get_latest_snapshots_by_game()
        injuries   = get_recent_injuries()
        game_pool  = []

        # Cap games processed per run to avoid OOM on 1GB VPS.
        snapshot_items = list(snapshots.items())[:20]

        for game_id, snap_list in snapshot_items:
            if not snap_list:
                continue
            best_snap = snap_list[0]
            sport_key = best_snap.get("sport_key", "")
            home_team = best_snap.get("home_team", "")
            away_team = best_snap.get("away_team", "")

            # Use Sofascore's exact kick-off time if available, else fall back to Odds API
            commence  = _sf_enrich(home_team, away_team, str(best_snap.get("commence_time", "")))
            from datetime import datetime as _dt
            import zoneinfo as _zi
            _today = _dt.now(_zi.ZoneInfo("America/New_York")).strftime("%A %B %-d, %Y")
            event  = {"sport_key": sport_key, "home_team": home_team,
                      "away_team": away_team, "commence_time": commence,
                      "game_date": _today}
            game_injuries = [i for i in injuries if i.get("team") in (home_team, away_team)]
            odds_by_book  = {s["book"]: s["best_odds"] for s in snap_list if "book" in s}
            try:
                game_context = build_game_context(sport_key=sport_key, home_team=home_team,
                                                   away_team=away_team, game_time=commence)
            except Exception:
                game_context = {}
            hub_injuries = (game_context.get("injuries_espn_home", []) +
                            game_context.get("rotowire_injuries", []))
            all_injuries = hub_injuries or game_injuries
            hub_news     = game_context.get("news_espn", [])
            ai = analyse_pick(event, all_injuries, hub_news, odds_by_book, game_context)
            if not ai:
                continue
            best_odds_val = best_snap.get("best_odds", -110)
            opp_prob      = ai.get("opponent_probability")
            opponent_odds = (decimal_to_american(1.0 / opp_prob)
                             if opp_prob and 0 < opp_prob < 1 else None)
            confidence = compute_confidence(
                ai_win_prob         = ai.get("win_probability", 0.5),
                model_consensus     = ai.get("confidence", 0.5),
                line_movement_score = game_context.get("sharp_action", {}).get("score", 0.5),
                news_impact_score   = game_context.get("news_impact_score", 0.5),
                sport               = sport_key,
                market              = "h2h",
            )
            if confidence.calibrated_score < 0.765:
                continue
            # Use calibrated score for EV so edge reflects realistic win probability
            ev_result  = evaluate(american_odds=best_odds_val,
                                   projected_prob=confidence.calibrated_score,
                                   opponent_odds=opponent_odds)
            # Require positive EV — our win probability must beat the market
            if ev_result.ev_pct <= 0 or ev_result.projected_prob <= ev_result.no_vig_prob:
                continue
            market    = ai.get("market", best_snap.get("market", "h2h"))
            selection = ai.get("selection", "")
            books_odds = {s["book"]: s["best_odds"] for s in snap_list
                          if s.get("market") == market and s.get("selection") == selection
                          and s.get("book")}
            if not books_odds:
                books_odds = odds_by_book
            factors   = ai.get("key_factors") or []
            reasoning = (ai.get("reasoning") or "").strip()
            insight   = factors[0] if factors else (reasoning.split(".")[0][:90] if reasoning else "")
            game_pool.append({
                "type":         "game",
                "score":        confidence.calibrated_score * (1 + ev_result.ev_pct),
                "game_key":     f"{home_team}:{away_team}".lower(),
                "sport_key":    sport_key,
                "home_team":    home_team,
                "away_team":    away_team,
                "commence_time":commence,
                "market":       market,
                "selection":    selection,
                "best_odds":    best_odds_val,
                "best_book":    best_snap.get("book", "hardrock"),
                "books_odds":   books_odds,
                "ev_pct":       ev_result.ev_pct,
                "confidence":   confidence.calibrated_score,
                "units":        ev_result.units,
                "insight":      insight,
                "injuries":     len([i for i in all_injuries if i.get("status") in ("out", "doubtful")]),
            })

        # -- 2. Score player prop picks from Redis ------------------------
        prop_pool = []
        try:
            raw_props = r.get("props:odds_api")
            all_props = json.loads(raw_props) if raw_props else []
            for prop in all_props:
                player    = (prop.get("player") or prop.get("subject") or "").strip()
                stat      = prop.get("stat", "")
                sport_key = prop.get("sport_key", "")
                event_id  = prop.get("event_id", "")
                line      = prop.get("line")
                if not player or not stat:
                    continue
                over_odds  = prop.get("over_odds", {})
                under_odds = prop.get("under_odds", {})
                best_over  = max(over_odds.values(),  default=None) if over_odds  else None
                best_under = max(under_odds.values(), default=None) if under_odds else None
                direction, best_odds_val, all_book_odds = None, None, {}
                if best_over is not None and (best_under is None or best_over >= best_under):
                    direction, best_odds_val = "Over", best_over
                    all_book_odds = {f"{bk} Over": v for bk, v in over_odds.items()}
                elif best_under is not None:
                    direction, best_odds_val = "Under", best_under
                    all_book_odds = {f"{bk} Under": v for bk, v in under_odds.items()}
                if direction is None or best_odds_val is None:
                    continue
                conf = implied_prob(best_odds_val)
                if conf < 0.65:
                    continue
                # Vig-remove using both sides — if opposite side available use it, else estimate
                opp_odds_val = (max(under_odds.values()) if direction == "Over" and under_odds
                                else max(over_odds.values()) if direction == "Under" and over_odds
                                else None)
                prop_ev = evaluate(american_odds=best_odds_val, projected_prob=conf,
                                   opponent_odds=opp_odds_val)
                # Require positive EV — our estimated prob must beat the no-vig market prob
                if prop_ev.ev_pct <= 0 or conf <= prop_ev.no_vig_prob:
                    continue
                is_team = prop.get("is_team_prop", False)
                prop_pool.append({
                    "type":         "prop",
                    "score":        conf * (1 + prop_ev.ev_pct),
                    "game_key":     event_id,
                    "player":       player,
                    "stat":         stat,
                    "line":         line,
                    "direction":    direction,
                    "sport_key":    sport_key,
                    "event_id":     event_id,
                    "best_odds":    best_odds_val,
                    "books_odds":   all_book_odds,
                    "confidence":   conf,
                    "ev_pct":       prop_ev.ev_pct,
                    "units":        float(prop_ev.units or 1),
                    "is_team_prop": is_team,
                })
        except Exception as pe:
            logger.warning("Props pool build failed: %s", pe)

        # -- 3. Merge, deduplicate, select top 2-5 -----------------------
        pool  = sorted(game_pool + prop_pool, key=lambda x: x["score"], reverse=True)
        entry = []
        blocked_game_keys = set()
        seen_players      = set()
        for pick in pool:
            if len(entry) == 2:
                break
            if pick["type"] == "prop":
                if pick["game_key"] and pick["game_key"] in blocked_game_keys:
                    continue
                if pick["player"].lower() in seen_players:
                    continue
                seen_players.add(pick["player"].lower())
            else:
                if pick["game_key"] in blocked_game_keys:
                    continue
                blocked_game_keys.add(pick["game_key"])
            entry.append(pick)

        if len(entry) < 1:
            logger.info("generate_picks: no qualifying picks today")
            return {"picks": 0, "posted": False}

        # -- 4. Only post if entry changed --------------------------------
        entry_hash = hashlib.md5(json.dumps(
            [{"t": p["type"], "s": round(p["score"], 4),
              "k": p.get("selection") or f"{p.get('player')} {p.get('direction')}"}
             for p in entry], sort_keys=True).encode()).hexdigest()
        if r.get("picks:last_hash") == entry_hash:
            return {"picks": len(entry), "posted": False}
        r.setex("picks:last_hash", 21600, entry_hash)  # 6h — prevents re-post spam

        # -- 5. Build and post Discord embed -----------------------------
        _MARKET  = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}
        now_et   = datetime.now(ET)
        date_str = now_et.strftime("%A, %B %-d")
        now_str  = now_et.strftime("%I:%M %p ET")

        def _fmt(v):
            return f"+{v}" if isinstance(v, (int, float)) and v > 0 else str(v)

        def _hr_odds(books_odds, best_odds_val):
            """Return HardRock odds if available, otherwise best available odds."""
            for key, odds in books_odds.items():
                if "hardrock" in key.lower():
                    return odds
            return best_odds_val

        def _gt(commence):
            try:
                from dateutil.parser import parse as _p
                return _p(commence).astimezone(ET).strftime("%-I:%M %p ET")
            except Exception:
                return ""

        def _conf_bar(conf_pct):
            filled = round(conf_pct / 10)
            return "🟢" * filled + "⚫" * (10 - filled)

        pick_fields = []
        for i, p in enumerate(entry, 1):
            conf  = round(p["confidence"] * 100)
            sport = p["sport_key"]
            bar   = _conf_bar(conf)

            if p["type"] == "prop":
                hr_odds   = _hr_odds(p["books_odds"], p["best_odds"])
                prop_tag  = "🏟️ Team Prop" if p.get("is_team_prop") else "👤 Player Prop"
                pick_fields.append({
                    "name":  f"{i}. {get_emoji(sport)} {p['player']}  ·  {p['stat']} {p['direction']} {p['line']}  `{_fmt(hr_odds)}`",
                    "value": f"{get_name(sport)}  ·  {prop_tag}  ·  **{conf}%** {bar}  ·  📍 HardRock",
                    "inline": False,
                })
            else:
                mkt     = _MARKET.get(p["market"], p["market"].upper())
                hr_odds = _hr_odds(p["books_odds"], p["best_odds"])
                gt      = _gt(p["commence_time"])
                insight = p.get("insight", "")
                val = f"{get_name(sport)} · {mkt}: **{p['selection']}** `{_fmt(hr_odds)}`{f' · 🕐 {gt}' if gt else ''}\n**{conf}%** {bar} · 📍 HardRock{f'  · 💡 {insight}' if insight else ''}"
                pick_fields.append({
                    "name": f"{i}. {get_emoji(sport)} {p['away_team']} @ {p['home_team']}",
                    "value": val,
                    "inline": False,
                })

        avg_conf = round(sum(p["confidence"] for p in entry) / len(entry) * 100)

        # Color based on avg confidence
        if avg_conf >= 75:
            color = 0x1B5E20   # deep green
        elif avg_conf >= 65:
            color = 0x2E7D32   # green
        elif avg_conf >= 60:
            color = 0xF9A825   # amber
        else:
            color = 0xE65100   # orange

        embed = {
            "title":       f"🏆  Daily Picks  —  {date_str}",
            "description": f"**{len(entry)} pick{'s' if len(entry) > 1 else ''}**  ·  Avg confidence **{avg_conf}%**  ·  {now_str}",
            "color":       color,
            "fields":      pick_fields,
            "footer":      {"text": "HardRock Sportsbook  ·  AI deep research  ·  Bet responsibly"},
        }

        _run_async(_post({"embeds": [embed]}))
        logger.info("generate_picks: posted %d picks", len(entry))
        return {"picks": len(entry), "posted": True}

    except Exception as exc:
        logger.error("generate_picks failed: %s", exc)
        raise


def _score_kalshi_markets(markets: list[dict], top_n: int = 6) -> list[dict]:
    """
    AI-score Kalshi sports prediction markets.
    Returns top N markets with YES/NO recommendation and reasoning.
    """
    import json
    from src.engines.ai_engine import _call_json

    if not markets:
        return []

    # Only sports markets with meaningful volume
    sports = [m for m in markets if m.get("volume", 0) > 0 or m.get("yes_price")]
    if not sports:
        return markets[:top_n]

    system = """You are a sports prediction expert. Given a list of Kalshi prediction markets
(YES/NO contracts on sports outcomes), identify the best bets.

Return ONLY valid JSON array:
[
  {
    "index": <int>,
    "direction": "yes"|"no",
    "confidence": <float 0.0-1.0>,
    "reasoning": "<1-2 sentences>",
    "ev_pct": <float e.g. 0.05>
  }
]

Only include markets where you have genuine edge (confidence >= 0.60).
Consider: implied probability vs your assessment, team form, injuries, matchup."""

    compact = [
        {"index": i, "title": m.get("title", ""), "yes_price": m.get("yes_price"),
         "no_price": m.get("no_price"), "category": m.get("category", "")}
        for i, m in enumerate(sports[:20])
    ]

    try:
        result = _call_json(
            f"Score these Kalshi sports markets:\n\n```json\n{json.dumps(compact, indent=2)}\n```",
            system,
        )
        if not result or not isinstance(result, list):
            return sports[:top_n]

        scored = []
        for item in result:
            idx = item.get("index")
            if idx is None or idx >= len(sports):
                continue
            m = dict(sports[idx])
            m["ai_direction"]  = item.get("direction", "yes")
            m["ai_confidence"] = float(item.get("confidence", 0.6))
            m["ai_reasoning"]  = item.get("reasoning", "")
            m["ai_ev_pct"]     = float(item.get("ev_pct", 0))
            scored.append(m)

        scored.sort(key=lambda x: x.get("ai_confidence", 0), reverse=True)
        return scored[:top_n] if scored else sports[:top_n]
    except Exception as e:
        logger.warning("Kalshi AI scoring failed: %s", e)
        return sports[:top_n]


def _get_parlay_senders():
    from src.workers.alert_worker import send_hardrock_parlay_alert
    return send_hardrock_parlay_alert


def scan_todays_games():
    """
    8 AM daily: full Sofascore scan across all sports.
    Splits today's games into DAY (before 6 PM ET) and NIGHT (6 PM ET+).
    Caches both lists in Redis for the entry generators to use.
    """
    from src.apis.sofascore import SPORT_MAP, get_scheduled_events
    from src.core.timezone import et_naive
    from src.core.config import REDIS_URL
    from concurrent.futures import ThreadPoolExecutor
    import json, zoneinfo
    from datetime import datetime
    import redis as _redis

    ET = zoneinfo.ZoneInfo("America/New_York")
    today = et_naive().strftime("%Y-%m-%d")

    def _fetch(sport_key: str) -> list:
        try:
            return get_scheduled_events(sport_key, today)
        except Exception:
            return []

    day_games: list[dict] = []
    night_games: list[dict] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch, sk): sk for sk in SPORT_MAP}
        for fut in futures:
            for ev in fut.result():
                ct = ev.get("commence_time", "")
                is_night = False
                try:
                    from dateutil.parser import parse as _parse
                    t = _parse(ct).astimezone(ET) if ct else None
                    if t and t.hour >= 18:
                        is_night = True
                except Exception:
                    pass
                (night_games if is_night else day_games).append(ev)

    r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    r.setex("sofascore:day_games",   86400, json.dumps(day_games))
    r.setex("sofascore:night_games", 86400, json.dumps(night_games))

    # Store all today's games in a flat lookup keyed by lowercased team name
    all_today = day_games + night_games
    team_index: dict[str, dict] = {}
    for ev in all_today:
        for field in ("home_team", "away_team"):
            name = (ev.get(field) or "").lower().strip()
            if name:
                team_index[name] = ev
    r.setex("sofascore:today_index", 86400, json.dumps(team_index))

    logger.info("Today's games scan: %d day, %d night, %d total (%d teams indexed)",
                len(day_games), len(night_games), len(all_today), len(team_index))
    return {"day": len(day_games), "night": len(night_games), "total": len(all_today)}


def _load_todays_games(period: str) -> list[dict]:
    """Load day or night games from Redis cache (populated by scan_todays_games)."""
    from src.core.config import REDIS_URL
    import json, redis as _redis
    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = r.get(f"sofascore:{period}_games")
        return json.loads(raw) if raw else []
    except Exception as e:
        logger.warning("Could not load %s games from Redis: %s", period, e)
        return []


def _team_matches(team: str, sofascore_events: list[dict]) -> bool:
    """True if this team name fuzzy-matches any team in the Sofascore event list."""
    tl = team.lower()
    for ev in sofascore_events:
        for field in ("home_team", "away_team"):
            tok = ev.get(field, "").lower()
            if tok and (tl == tok or tl in tok or tok in tl):
                return True
    return False


def _build_hardrock_candidates(
    period: str,
    sofascore_events: list[dict],
) -> list[dict]:
    """
    Score all today's games that match the given period (day/night).
    Returns a flat list of scored candidate dicts.
    """
    from src.engines.odds_engine import get_latest_snapshots_by_game
    from src.engines.news_engine import get_recent_injuries
    from src.engines.confidence_engine import compute_confidence
    from src.engines.ev_engine import evaluate, decimal_to_american
    from src.engines.ai_engine import analyse_pick
    from src.apis.data_hub import build_game_context

    snapshots = get_latest_snapshots_by_game()
    injuries  = get_recent_injuries()
    candidates: list[dict] = []
    _top_seen_conf: float = 0.0  # track best calibrated score even for rejected picks

    # Cap at 25 games per HardRock entry build to stay within 1GB RAM
    for game_id, snap_list in list(snapshots.items())[:25]:
        if not snap_list:
            continue
        best_snap = snap_list[0]
        sport_key = best_snap.get("sport_key", "")
        home_team = best_snap.get("home_team", "")
        away_team = best_snap.get("away_team", "")
        commence  = str(best_snap.get("commence_time", ""))

        # Day/night filter — Sofascore cache first, fall back to commence_time
        if sofascore_events:
            if not (_team_matches(home_team, sofascore_events) or _team_matches(away_team, sofascore_events)):
                continue
        else:
            # Sofascore unavailable — use commence_time directly for period split
            # Day = before 18:00 ET, Night = 18:00 ET and later
            try:
                from dateutil.parser import parse as _parse
                import zoneinfo as _zi
                _ct = _parse(commence)
                if _ct.tzinfo is None:
                    _ct = _ct.replace(tzinfo=__import__('datetime').timezone.utc)
                _ct_et = _ct.astimezone(_zi.ZoneInfo("America/New_York"))
                _is_night_game = _ct_et.hour >= 18
                if period == "day" and _is_night_game:
                    continue
                if period == "night" and not _is_night_game:
                    continue
            except Exception:
                pass  # unparseable time — include in both periods

        event        = {"sport_key": sport_key, "home_team": home_team, "away_team": away_team, "commence_time": commence}
        game_injuries = [i for i in injuries if i.get("team") in (home_team, away_team)]
        odds_by_book  = {s["book"]: s["best_odds"] for s in snap_list if "book" in s}

        try:
            game_context = build_game_context(sport_key=sport_key, home_team=home_team, away_team=away_team, game_time=commence)
        except Exception:
            game_context = {}

        hub_injuries = game_context.get("injuries_espn_home", []) + game_context.get("rotowire_injuries", [])
        all_injuries = hub_injuries or game_injuries
        hub_news     = game_context.get("news_espn", [])

        ai = analyse_pick(event, all_injuries, hub_news, odds_by_book, game_context)
        if not ai:
            continue

        best_odds_val = best_snap.get("best_odds", -110)
        opp_prob      = ai.get("opponent_probability")
        opponent_odds = decimal_to_american(1.0 / opp_prob) if opp_prob and 0 < opp_prob < 1 else None

        confidence = compute_confidence(
            ai_win_prob         = ai.get("win_probability", 0.5),
            model_consensus     = ai.get("confidence", 0.5),
            line_movement_score = game_context.get("sharp_action", {}).get("score", 0.5),
            news_impact_score   = game_context.get("news_impact_score", 0.5),
            sport               = sport_key,
            market              = "h2h",
        )

        _top_seen_conf = max(_top_seen_conf, confidence.calibrated_score)
        if confidence.calibrated_score < 0.765:
            logger.info(
                "PASS [%s vs %s] conf=%.1f%% ai_prob=%.1f%% — below threshold",
                home_team, away_team,
                confidence.calibrated_score * 100,
                ai.get("win_probability", 0) * 100,
            )
            continue

        # Use calibrated score (not raw AI prob) so EV reflects realistic edge
        ev_result = evaluate(american_odds=best_odds_val, projected_prob=confidence.calibrated_score, opponent_odds=opponent_odds)

        # Require genuine edge — win probability must beat the vig-free market probability
        if ev_result.ev_pct <= 0 or ev_result.projected_prob <= ev_result.no_vig_prob:
            continue

        market    = ai.get("market", best_snap.get("market", "h2h"))
        selection = ai.get("selection", "")
        books_odds = {s["book"]: s["best_odds"] for s in snap_list if s.get("market") == market and s.get("selection") == selection and s.get("book")}
        if not books_odds:
            books_odds = odds_by_book

        candidates.append({
            "type":         "team",
            "score":        confidence.calibrated_score * (1 + ev_result.ev_pct),
            "sport_key":    sport_key,
            "home_team":    home_team,
            "away_team":    away_team,
            "commence_time":commence,
            "market":       market,
            "selection":    selection,
            "best_odds":    best_odds_val,
            "best_book":    best_snap.get("book", "hardrock"),
            "books_odds":   books_odds,
            "ev_pct":       ev_result.ev_pct,
            "confidence":   confidence.calibrated_score,
            "units":        ev_result.units,
            "reasoning":    ai.get("reasoning", ""),
            "key_factors":  ai.get("key_factors", []),
            "injuries":     len([i for i in all_injuries if i.get("status") in ("out", "doubtful")]),
        })

    # Attach best-seen confidence so caller can report it even when no picks qualify
    if not candidates:
        candidates.append({"_meta": True, "_top_conf": _top_seen_conf})
    else:
        candidates[0]["_top_conf"] = _top_seen_conf
    return candidates


def _build_prop_candidates(sofascore_events: list[dict]) -> list[dict]:
    """
    Pull player/team props from Redis, pre-screen by odds, then run AI deep research
    on the top 8 candidates. Confidence comes from AI analysis, not just implied prob.
    """
    from src.core.config import REDIS_URL
    from src.engines.ev_engine import implied_prob, evaluate
    from src.engines.ai_engine import analyse_pick
    from src.apis.data_hub import build_player_context
    import json, redis as _redis

    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = r.get("props:odds_api")
        all_props: list[dict] = json.loads(raw) if raw else []
    except Exception:
        return []

    # Phase 1: odds-based pre-screen (fast, no AI calls)
    pre_screened: list[dict] = []
    for prop in all_props:
        player    = (prop.get("player") or prop.get("subject") or "").strip()
        stat      = prop.get("stat", "")
        sport_key = prop.get("sport_key", "")
        event_id  = prop.get("event_id", "")
        line      = prop.get("line")
        if not player or not stat:
            continue

        over_odds  = prop.get("over_odds", {})
        under_odds = prop.get("under_odds", {})
        best_over  = max(over_odds.values(),  default=None) if over_odds  else None
        best_under = max(under_odds.values(), default=None) if under_odds else None

        direction, best_odds_val, all_book_odds = None, None, {}
        if best_over is not None and (best_under is None or best_over >= best_under):
            direction, best_odds_val = "Over",  best_over
            all_book_odds = {f"{bk} Over": v for bk, v in over_odds.items()}
        elif best_under is not None:
            direction, best_odds_val = "Under", best_under
            all_book_odds = {f"{bk} Under": v for bk, v in under_odds.items()}

        if direction is None or best_odds_val is None:
            continue

        raw_prob = implied_prob(best_odds_val)
        if raw_prob < 0.58:   # wider pre-screen — AI may find edge the market missed
            continue

        opp_odds_val = (max(under_odds.values()) if direction == "Over" and under_odds
                        else max(over_odds.values()) if direction == "Under" and over_odds
                        else None)
        prop_ev = evaluate(american_odds=best_odds_val, projected_prob=raw_prob,
                           opponent_odds=opp_odds_val)

        pre_screened.append({
            "type":         "prop",
            "player":       player,
            "stat":         stat,
            "line":         line,
            "direction":    direction,
            "sport_key":    sport_key,
            "event_id":     event_id,
            "best_odds":    best_odds_val,
            "books_odds":   all_book_odds,
            "raw_prob":     raw_prob,
            "ev_pct":       prop_ev.ev_pct,
            "units":        float(prop_ev.units or 1),
            "is_team_prop": prop.get("is_team_prop", False),
            "home_team":    prop.get("home_team", ""),
            "away_team":    prop.get("away_team", ""),
        })

    # Sort by implied prob descending, take top 8 for AI research
    pre_screened.sort(key=lambda x: x["raw_prob"], reverse=True)

    candidates: list[dict] = []
    for prop in pre_screened[:8]:
        player    = prop["player"]
        stat      = prop["stat"]
        sport_key = prop["sport_key"]
        direction = prop["direction"]
        line      = prop["line"]

        # Build player context (recent form, vs-opponent)
        opponent = prop.get("away_team") or prop.get("home_team") or ""
        try:
            player_ctx = build_player_context(player, sport_key, opponent=opponent, n_games=5)
        except Exception:
            player_ctx = {}

        # Run AI deep research on this prop
        event = {
            "sport_key":      sport_key,
            "home_team":      prop.get("home_team", ""),
            "away_team":      prop.get("away_team", ""),
            "commence_time":  "",
            "prop_player":    player,
            "prop_stat":      stat,
            "prop_line":      line,
            "prop_direction": direction,
            "description":    f"{player} {stat} {direction} {line}",
        }
        ai = analyse_pick(event, [], [], {direction: prop["best_odds"]}, player_ctx)

        if not ai or ai.get("recommendation") == "PASS" or not ai.get("should_bet", True):
            continue

        conf      = ai.get("win_probability", prop["raw_prob"])
        ev        = ai.get("ev_pct", prop["ev_pct"])
        reasoning = ai.get("reasoning", "")

        if ev <= 0:
            continue

        candidates.append({
            "type":         "prop",
            "score":        conf * (1 + ev),
            "player":       player,
            "stat":         stat,
            "line":         line,
            "direction":    direction,
            "sport_key":    sport_key,
            "event_id":     prop["event_id"],
            "best_odds":    prop["best_odds"],
            "books_odds":   prop["books_odds"],
            "confidence":   conf,
            "ev_pct":       ev,
            "units":        prop["units"],
            "is_team_prop": prop["is_team_prop"],
            "reasoning":    reasoning,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _post_hardrock_embed(period: str, entry: list[dict]) -> None:
    """Build and post the HardRock Discord embed — individual picks, no parlay framing."""
    from src.core.sport_labels import get_emoji
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    import zoneinfo, hashlib
    from datetime import datetime

    ET           = zoneinfo.ZoneInfo("America/New_York")
    now_et       = datetime.now(ET)
    date_str     = now_et.strftime("%b %-d, %Y")
    period_label = "DAY ENTRY" if period == "day" else "NIGHT ENTRY"
    period_emoji = "☀️" if period == "day" else "🌙"
    _MARKET_BADGE = {"h2h": "MONEYLINE", "spreads": "SPREAD", "totals": "TOTAL"}
    slip_id      = hashlib.md5(f"{period}{date_str}".encode()).hexdigest()[:8].upper()

    def _fmt(v) -> str:
        return f"+{v}" if isinstance(v, (int, float)) and v > 0 else str(v)

    def _game_time(commence: str) -> str:
        try:
            from dateutil.parser import parse as _p
            return _p(commence).astimezone(ET).strftime("%-I:%M %p ET")
        except Exception:
            return ""

    pick_fields = []
    for i, p in enumerate(entry, 1):
        conf     = round(p["confidence"] * 100)
        ev       = round(p.get("ev_pct", 0) * 100, 1)
        emoji    = get_emoji(p["sport_key"])
        odds_fmt = _fmt(p["best_odds"])
        reasoning = p.get("reasoning", "")
        reason_short = (reasoning.split(".")[0].strip()[:120] + "…") if reasoning else ""

        if p["type"] == "prop":
            label    = "TEAM PROP" if p.get("is_team_prop") else "PLAYER PROP"
            direction = p["direction"].upper()
            pick_fields.append({
                "name":  f"PICK {i}  ·  {emoji}  `{label}`",
                "value": (
                    f"**{p['player']}**  —  {p['stat']} **{direction} {p['line']}**\n"
                    f"Odds  `{odds_fmt}`   Conf  **{conf}%**   Edge  **+{ev}%**"
                    + (f"\n> _{reason_short}_" if reason_short else "")
                ),
                "inline": False,
            })
        else:
            badge = _MARKET_BADGE.get(p["market"], p["market"].upper())
            gt    = _game_time(p.get("commence_time", ""))
            inj   = "  ⚠️" if p.get("injuries", 0) > 0 else ""
            time_str = f"  ·  {gt}" if gt else ""
            pick_fields.append({
                "name":  f"PICK {i}  ·  {emoji}  `{badge}`",
                "value": (
                    f"**{p['away_team']} @ {p['home_team']}**{inj}{time_str}\n"
                    f"✅  **{p['selection']}**\n"
                    f"Odds  `{odds_fmt}`   Conf  **{conf}%**   Edge  **+{ev}%**"
                    + (f"\n> _{reason_short}_" if reason_short else "")
                ),
                "inline": False,
            })

    avg_conf = round(sum(p["confidence"] for p in entry) / len(entry) * 100)

    embed = {
        "title":       f"🎟️  HARDROCK BET  ·  {period_emoji} {period_label}",
        "description": f"**{date_str}**  ·  {len(entry)} Pick{'s' if len(entry) > 1 else ''}  ·  Slip `#{slip_id}`",
        "fields":      pick_fields + [{
            "name":  "─────────────────────────────",
            "value": f"Avg Confidence  **{avg_conf}%**  ·  Place each bet individually on HardRock",
            "inline": False,
        }],
        "color":  0xB71C1C,
        "footer": {"text": "HardRock Bet  ·  Bet responsibly  ·  Research-backed picks"},
    }

    _run_async(_post({"embeds": [embed]}))
    logger.info("HardRock %s entry posted: %d picks", period, len(entry))


def _generate_hardrock_entry(period: str) -> dict:
    """
    Core logic for day/night entries.

    Builds a unified pool of game picks + player props. Applies uniqueness:
    - Same game can only appear once (no ML + prop from same game)
    - No player appears twice
    Ranks by confidence score, takes 2–5 picks.
    Posts to Discord if at least 1 qualifying pick exists (max 5).
    """
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}

    # Dedup: only post once per period per day
    try:
        import redis as _redis
        from src.core.config import REDIS_URL
        from src.core.timezone import et_naive as _et_naive
        _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _today = _et_naive().strftime("%Y-%m-%d")
        _dedup_key = f"hardrock:posted:{period}:{_today}"
        if _r.get(_dedup_key):
            logger.info("HardRock %s entry already posted today — skipping duplicate", period)
            return {"skipped": "already_posted", "period": period}
    except Exception:
        pass  # Redis unavailable — allow through

    try:
        sofascore_events = _load_todays_games(period)
        if not sofascore_events:
            logger.info("HardRock %s entry: no Sofascore cache yet — proceeding anyway", period)

        raw_game  = _build_hardrock_candidates(period, sofascore_events)
        raw_props = _build_prop_candidates(sofascore_events)
        pool      = sorted(raw_game + raw_props, key=lambda x: x["score"], reverse=True)

        # Every pick must independently justify its inclusion.
        # For a parlay: combined win probability × combined payout must be > 1 (positive EV).
        # If adding a second leg makes the parlay EV negative, post the single instead.
        CONF_FLOOR = 0.765  # displays as 77%+ on Discord (0.765 rounds to 77)
        EV_FLOOR   = 0.005  # 0.5% minimum edge — even thin positive EV qualifies

        def _american_to_dec(odds: int) -> float:
            return (odds / 100 + 1) if odds > 0 else (100 / abs(odds) + 1)

        def _parlay_is_profitable(picks: list[dict]) -> bool:
            """Return True only if combined_win_prob × parlay_decimal_payout > 1."""
            combined_win_prob = 1.0
            combined_dec      = 1.0
            for p in picks:
                combined_win_prob *= p["confidence"]
                combined_dec      *= _american_to_dec(int(p["best_odds"]))
            return combined_win_prob * combined_dec > 1.0

        entry: list[dict]            = []
        blocked_event_keys: set[str] = set()
        seen_players: set[str]       = set()

        for pick in pool:
            if len(entry) == 2:
                break

            conf = pick["confidence"]
            ev   = pick.get("ev_pct", 0)

            if conf < CONF_FLOOR or ev < EV_FLOOR:
                continue

            # Every leg added must keep the parlay profitable as a whole.
            # If the combined win_prob × payout drops below 1.0, skip this leg.
            # This naturally prevents 3-leg parlays unless all 3 genuinely earn it.
            if entry and not _parlay_is_profitable(entry + [pick]):
                continue

            if pick["type"] == "prop":
                player_key = pick["player"].lower()
                event_key  = pick.get("event_id", "")
                if event_key and event_key in blocked_event_keys:
                    continue
                if player_key in seen_players:
                    continue
                seen_players.add(player_key)
            else:
                event_key = f"{pick['home_team']}:{pick['away_team']}".lower()
                if event_key in blocked_event_keys:
                    continue
                blocked_event_keys.add(event_key)

            entry.append(pick)

        if len(entry) < 1:
            # ── LAST RESORT: Perplexity web search ───────────────────────────
            # Only fires when everything else (Sofascore + Sportradar + ESPN +
            # Action Network) was not enough. Takes the top 2 closest candidates
            # and gives them one final chance with fresh web research.
            best_conf = round(max((p["confidence"] for p in pool), default=0) * 100)
            logger.info(
                "HardRock %s entry: no qualifying picks after all sources "
                "(best conf: %d%%) — trying Perplexity last resort",
                period, best_conf,
            )
            # Only trigger if a candidate is very close to the floor (0.70+, within 7 pts).
            # With Sportradar + Sofascore + ESPN already in the pipeline, confidence
            # should never need a web search rescue unless a candidate is nearly there.
            # AND we haven't already used Perplexity twice today.
            close_candidates = [
                p for p in pool
                if 0.70 <= p["confidence"] < CONF_FLOOR and p.get("ev_pct", 0) > 0
            ][:1]  # one candidate only — absolute last resort

            # Daily rate gate — max 2 Perplexity calls per day across all entries
            _perplexity_allowed = False
            try:
                from src.core.config import REDIS_URL, PERPLEXITY_API_KEY
                import redis as _redis
                from datetime import datetime as _dt
                if PERPLEXITY_API_KEY and close_candidates:
                    _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
                    _date_key = f"perplexity:calls:{_dt.utcnow().strftime('%Y-%m-%d')}"
                    _calls_today = int(_r.get(_date_key) or 0)
                    if _calls_today < 2:
                        _perplexity_allowed = True
            except Exception:
                pass

            if close_candidates and _perplexity_allowed:
                try:
                    from src.apis.websearch import search_game_news, search_player_news
                    from src.engines.ai_engine import analyse_pick as _analyse
                    from src.engines.confidence_engine import compute_confidence as _conf
                    from src.engines.ev_engine import evaluate as _ev, decimal_to_american as _d2a
                    from datetime import datetime as _dt
                    import zoneinfo as _zi
                    _today = _dt.now(_zi.ZoneInfo("America/New_York")).strftime("%B %d, %Y")

                    for candidate in close_candidates:
                        is_prop = candidate["type"] == "prop"

                        if is_prop:
                            web_news = search_player_news(
                                candidate["player"], candidate["stat"],
                                candidate["sport_key"],
                            )
                        else:
                            web_news = search_game_news(
                                candidate["home_team"], candidate["away_team"],
                                candidate["sport_key"], _today,
                            )

                        if not web_news:
                            continue

                        logger.info(
                            "Perplexity last resort [%s]: web search complete",
                            candidate.get("player") or
                            f"{candidate.get('away_team')} vs {candidate.get('home_team')}",
                        )

                        # Re-run AI with web findings injected
                        enriched_ctx = {"web_search_news": web_news}
                        if is_prop:
                            event = {
                                "sport_key":      candidate["sport_key"],
                                "prop_player":    candidate["player"],
                                "prop_stat":      candidate["stat"],
                                "prop_line":      candidate["line"],
                                "prop_direction": candidate["direction"],
                                "description":    f"{candidate['player']} {candidate['stat']} {candidate['direction']} {candidate['line']}",
                            }
                            ai2 = _analyse(event, [], [{"headline": web_news, "source": "perplexity"}],
                                           {candidate["direction"]: candidate["best_odds"]}, enriched_ctx)
                            if not ai2 or not ai2.get("should_bet") or ai2.get("recommendation") == "PASS":
                                continue
                            new_conf = ai2.get("win_probability", 0)
                            new_ev   = ai2.get("ev_pct", candidate["ev_pct"])
                        else:
                            event = {
                                "sport_key":    candidate["sport_key"],
                                "home_team":    candidate["home_team"],
                                "away_team":    candidate["away_team"],
                                "commence_time":candidate["commence_time"],
                            }
                            ai2 = _analyse(event, [], [{"headline": web_news, "source": "perplexity"}],
                                           candidate["books_odds"], enriched_ctx)
                            if not ai2 or not ai2.get("should_bet") or ai2.get("recommendation") == "PASS":
                                continue
                            conf_result = _conf(
                                ai_win_prob          = ai2.get("win_probability", 0.5),
                                model_consensus      = ai2.get("confidence", 0.5),
                                line_movement_score  = 0.5,
                                news_impact_score    = 0.5,
                                sport                = candidate["sport_key"],
                                market               = candidate.get("market", "h2h"),
                            )
                            new_conf = conf_result.calibrated_score
                            new_ev   = candidate["ev_pct"]

                        if new_conf >= CONF_FLOOR and new_ev >= EV_FLOOR:
                            updated = {
                                **candidate,
                                "confidence": new_conf,
                                "ev_pct":     new_ev,
                                "score":      new_conf * (1 + new_ev),
                                "reasoning":  ai2.get("reasoning", candidate.get("reasoning", "")),
                            }
                            entry.append(updated)
                            # Increment daily counter so we don't exceed 2 calls/day
                            try:
                                _r.incr(_date_key)
                                _r.expire(_date_key, 86400)
                            except Exception:
                                pass
                            logger.info(
                                "Perplexity last resort: %s qualified at conf=%.2f",
                                candidate.get("player") or
                                f"{candidate.get('away_team')} vs {candidate.get('home_team')}",
                                new_conf,
                            )
                            break  # one good pick is enough from last resort

                except Exception as _lr_err:
                    logger.warning("Perplexity last resort failed: %s", _lr_err)

            if len(entry) < 1:
                try:
                    from src.workers.alert_worker import _run_async
                    from src.discord_bot.bot import _post
                    period_emoji = "☀️" if period == "day" else "🌙"
                    period_label = "DAY ENTRY" if period == "day" else "NIGHT ENTRY"
                    _run_async(_post({"embeds": [{
                        "title": f"🎟️  HARDROCK SLIP  ·  {period_emoji} {period_label}",
                        "description": (
                            f"No qualifying picks after all research including web search.\n"
                            f"Best confidence found: **{best_conf}%** — not enough edge today.\n"
                            f"_Protecting bankroll — no edge, no bet._"
                        ),
                        "color": 0x546E7A,
                        "footer": {"text": "HardRock Bet  ·  Bet responsibly"},
                    }]}))
                except Exception as _e:
                    logger.warning("Could not post no-picks alert: %s", _e)
                return {"picks": 0, "period": period, "posted": False}

        _post_hardrock_embed(period, entry)

        try:
            from src.workers.slip_tracker import save_slip
            save_slip(period, "hardrock", entry)
        except Exception as e:
            logger.warning("slip_tracker.save_slip failed: %s", e)

        # Mark as posted in Redis so re-runs don't duplicate
        try:
            import redis as _redis
            from src.core.config import REDIS_URL
            from src.core.timezone import et_naive as _et_naive
            _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
            _today = _et_naive().strftime("%Y-%m-%d")
            _r.setex(f"hardrock:posted:{period}:{_today}", 86400, "1")
        except Exception:
            pass

        return {"period": period, "picks": len(entry), "posted": True}

    except Exception as exc:
        logger.error("HardRock %s entry failed: %s", period, exc)
        return {"error": str(exc)}



def generate_hardrock_day_entry():
    """Post the Day entry (games before 6 PM ET) — runs at 10 AM."""
    return _generate_hardrock_entry("day")


def generate_hardrock_night_entry():
    """Post the Night entry (games 6 PM ET+) — runs at 4 PM."""
    return _generate_hardrock_entry("night")


def generate_parlays():
    """Build and alert top parlay opportunities from today's BET picks."""
    try:
        from src.engines.parlay_engine import find_best_parlays
        from src.db.session import get_db
        from src.db.models import Pick
        from datetime import datetime, timedelta

        # Extract plain values inside session — avoids DetachedInstanceError after close
        with get_db() as db:
            rows = db.query(
                Pick.id, Pick.game_id, Pick.selection, Pick.sport,
                Pick.market, Pick.best_book, Pick.american_odds_at_gen,
                Pick.confidence_pct, Pick.ev_pct,
            ).filter(
                Pick.generated_at >= datetime.utcnow() - timedelta(hours=12),
                Pick.recommendation == "BET",
            ).all()

        if len(rows) < 2:
            return {"parlays": 0}

        from src.engines.parlay_engine import ParlayLeg
        parlay_legs = [
            ParlayLeg(
                event_id       = str(game_id or pid),
                event_name     = selection or "",
                sport          = sport or "",
                market         = market or "h2h",
                selection      = selection or "",
                book           = best_book or "",
                american_odds  = american_odds or -110,
                win_probability= (confidence_pct or 50) / 100.0,
                ev_pct         = ev_pct or 0.0,
                confidence     = (confidence_pct or 50) / 100.0,
            )
            for pid, game_id, selection, sport, market, best_book, american_odds, confidence_pct, ev_pct in rows
        ]
        parlays = find_best_parlays(parlay_legs, max_legs=4, top_n=3)

        if parlays:
            from src.workers.alert_worker import send_parlay_alerts
            import dataclasses
            send_parlay_alerts([dataclasses.asdict(p) for p in parlays])

        return {"parlays": len(parlays)}

    except Exception as exc:
        logger.error("Parlay generation failed: %s", exc)
        return {"error": str(exc)}


