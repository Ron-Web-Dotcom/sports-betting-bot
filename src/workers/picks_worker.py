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

logger = logging.getLogger(__name__)

# Floors — never lower these
CONF_FLOOR = 0.765  # 77%+ calibrated confidence for -odds picks
EV_FLOOR   = 0.005  # 0.5% minimum expected value


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
        import redis as _redis
        import json
        import hashlib
        import zoneinfo
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
            odds_by_book  = {s["book"]: s["best_odds"] for s in snap_list if s.get("book") and s.get("best_odds") is not None}
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
            market    = ai.get("market", best_snap.get("market", "h2h"))
            selection = ai.get("selection", "")
            books_odds = {s["book"]: s["best_odds"] for s in snap_list
                          if s.get("market") == market and s.get("selection") == selection
                          and s.get("book") and s.get("best_odds") is not None}
            if not books_odds:
                # Fuzzy fallback: AI may return "White Sox" instead of "Chicago White Sox"
                # Try partial match against snapshot selections
                for s in snap_list:
                    s_sel = (s.get("selection") or "").lower()
                    s_mkt = s.get("market", "")
                    if s_mkt == market and s.get("book") and (
                        selection.lower() in s_sel or s_sel in selection.lower()
                    ):
                        if s.get("best_odds") is not None:
                            books_odds[s["book"]] = s["best_odds"]
            if not books_odds:
                logger.warning("picks: no odds matched selection=%r market=%r for %s @ %s — falling back to game odds", selection, market, away_team, home_team)
                books_odds = odds_by_book
            # Derive best_odds from AI's actual selection — not snapshot[0] which may be the other side
            def _odds_sort_key(v):
                if v > 0: return v
                denom = 100 + abs(v)
                return 1 / (1 - 100 / denom) if denom != 100 else -999
            best_odds_val = max(books_odds.values(), key=_odds_sort_key) if books_odds else best_snap.get("best_odds", -110)
            opp_prob      = ai.get("opponent_probability")
            opponent_odds = (decimal_to_american(1.0 / opp_prob)
                             if opp_prob and 0 < opp_prob < 1 else None)
            confidence = compute_confidence(
                ai_win_prob         = ai.get("win_probability", 0.5),
                model_consensus     = ai.get("confidence", 0.5),
                line_movement_score = game_context.get("sharp_action", {}).get("score", 0.5),
                news_impact_score   = game_context.get("news_impact_score", 0.5),
                sport               = sport_key,
                market              = market,
            )
            # Dual-path gate: +odds needs 42%+ AND 5% edge over implied; -odds needs 77%+ all markets
            _cal = confidence.calibrated_score
            _min_prob = CONF_FLOOR
            if best_odds_val > 0:
                _implied = 100 / (100 + best_odds_val)
                if _cal < 0.42 or (_cal - _implied) < 0.05:
                    continue
            else:
                if _cal < _min_prob:
                    continue
            # Use calibrated score for EV so edge reflects realistic win probability
            ev_result  = evaluate(american_odds=best_odds_val,
                                   projected_prob=_cal,
                                   opponent_odds=opponent_odds)
            # Require positive EV — skip no-vig check when opponent_odds unknown (avoids blocking favorites)
            if ev_result.ev_pct < EV_FLOOR:
                continue
            if opponent_odds is not None and _cal <= ev_result.no_vig_prob:
                continue
            factors   = ai.get("key_factors") or []
            reasoning = (ai.get("reasoning") or "").strip()
            insight   = factors[0] if factors else (reasoning.split(".")[0][:90] if reasoning else "")
            # Favorite bonus: -odds picks get a small score boost to bubble up in slip builder.
            # Keeps confidence×EV as the core but prefers stackable favorites for parlay value.
            # Close-match penalty: both sides + odds = coin flip / draw risk, rank lower.
            _both_plus = best_odds_val > 0 and (opponent_odds is not None and opponent_odds > 0)
            _fav_bonus = 0.04 if best_odds_val < -120 else (0.02 if best_odds_val < 0 else (-0.06 if _both_plus else 0.0))
            game_pool.append({
                "type":         "game",
                "score":        confidence.calibrated_score * (1 + ev_result.ev_pct) * (1 + _fav_bonus),
                "game_key":     f"{home_team}:{away_team}".lower(),
                "sport_key":    sport_key,
                "home_team":    home_team,
                "away_team":    away_team,
                "commence_time":commence,
                "market":       market,
                "selection":    selection,
                "best_odds":    best_odds_val,
                "best_book":    next((bk for bk, v in books_odds.items() if v == best_odds_val), best_snap.get("book", "hardrock")),
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
                # Pick the side with HIGHER implied probability (more likely outcome)
                direction, best_odds_val, all_book_odds = None, None, {}
                ip_over  = implied_prob(best_over)  if best_over  is not None else 0.0
                ip_under = implied_prob(best_under) if best_under is not None else 0.0
                if best_over is not None and ip_over >= ip_under:
                    direction, best_odds_val = "Over", best_over
                    all_book_odds = {f"{bk} Over": v for bk, v in over_odds.items()}
                elif best_under is not None:
                    direction, best_odds_val = "Under", best_under
                    all_book_odds = {f"{bk} Under": v for bk, v in under_odds.items()}
                if direction is None or best_odds_val is None:
                    continue
                conf = implied_prob(best_odds_val)
                # Dual-path gate for props — +odds needs 42%+ AND 5% edge; -odds needs 60%+
                opp_odds_val = (max(under_odds.values()) if direction == "Over" and under_odds
                                else max(over_odds.values()) if direction == "Under" and over_odds
                                else None)
                if best_odds_val > 0:
                    _prop_implied = 100 / (100 + best_odds_val)
                    if conf < 0.42 or (conf - _prop_implied) < 0.05:
                        continue
                else:
                    if conf < 0.60:
                        continue
                prop_ev = evaluate(american_odds=best_odds_val, projected_prob=conf,
                                   opponent_odds=opp_odds_val)
                # Require positive EV — skip no-vig check when opposite side unknown
                if prop_ev.ev_pct < EV_FLOOR:
                    continue
                if opp_odds_val is not None and conf <= prop_ev.no_vig_prob:
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
            if idx is None or idx < 0 or idx >= len(sports):
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
    8 AM daily: scan today's games via Sofascore (routed through ScraperAPI when
    Decodo is blocked). Sofascore is the single source of truth for global game
    discovery — ScraperAPI ensures it never fails.
    Splits into DAY (before 4 PM ET) and NIGHT (4 PM ET+).
    Caches both lists in Redis for the entry generators to use.
    """
    from src.apis.sofascore import get_all_scheduled_events
    from src.core.timezone import et_naive
    from src.core.config import REDIS_URL
    import json
    import zoneinfo
    import redis as _redis

    ET = zoneinfo.ZoneInfo("America/New_York")
    today = et_naive().strftime("%Y-%m-%d")

    day_games: list[dict] = []
    night_games: list[dict] = []
    seen_ids: set[str] = set()
    seen_pairs: set[str] = set()

    def _add_events(events: list):
        added = 0
        for ev in events:
            eid = str(ev.get("id", ""))
            home = (ev.get("home_team") or "").lower().strip()
            away = (ev.get("away_team") or "").lower().strip()
            pair = f"{home}|{away}" if home and away else ""

            if eid and eid in seen_ids:
                continue
            if pair and pair in seen_pairs:
                continue
            if eid:
                seen_ids.add(eid)
            if pair:
                seen_pairs.add(pair)

            ct = ev.get("commence_time", "")
            is_night = False
            try:
                from dateutil.parser import parse as _parse
                t = _parse(ct) if ct else None
                if t:
                    if t.tzinfo is not None:
                        t = t.astimezone(ET)
                    else:
                        t = t.replace(tzinfo=ET)
                    if t.hour >= 16:
                        is_night = True
            except Exception:
                pass
            (night_games if is_night else day_games).append(ev)
            added += 1
        return added

    sf_count = 0
    try:
        sf_count = _add_events(get_all_scheduled_events(today))
    except Exception as e:
        logger.warning("scan_todays_games: Sofascore batch failed — %s", e)

    logger.info("Game scan complete: %d from Sofascore (%d day, %d night)",
                sf_count, len(day_games), len(night_games))

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
    """Load day or night games from Redis cache (populated by scan_todays_games).
    Rescans if cache is missing OR empty — guards against Sofascore returning 0 games at 8 AM."""
    from src.core.config import REDIS_URL
    import json
    import redis as _redis
    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = r.get(f"sofascore:{period}_games")
        games = json.loads(raw) if raw else []
        if not games:
            logger.info("Sofascore %s cache empty — running fresh scan", period)
            scan_todays_games()
            raw = r.get(f"sofascore:{period}_games")
            games = json.loads(raw) if raw else []
        return games
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

    Simplified gates — one pass, no re-applied checks:
      -odds: AI win_probability >= 77% AND simple EV > 0
      +odds: AI win_prob >= 42% AND edge over implied >= 5% AND EV > 0
    """
    from src.engines.odds_engine import get_latest_snapshots_by_game
    from src.engines.news_engine import get_recent_injuries
    from src.engines.ev_engine import evaluate, decimal_to_american, american_to_decimal
    from src.engines.ai_engine import analyse_pick
    from src.apis.data_hub import build_game_context
    import zoneinfo as _zi
    from dateutil.parser import parse as _dparse
    import datetime as _dt

    snapshots = get_latest_snapshots_by_game()
    injuries  = get_recent_injuries()
    candidates: list[dict] = []
    _top_seen_conf: float  = 0.0

    # Load Sofascore team index — keyed by lowercased team name → event dict (has 'id')
    _sf_index: dict[str, dict] = {}
    try:
        from src.core.config import REDIS_URL as _RURL_SF
        import redis as _redis_sf
        import json as _json_sf
        _rsf = _redis_sf.from_url(_RURL_SF, decode_responses=True, socket_connect_timeout=2)
        _raw_idx = _rsf.get("sofascore:today_index")
        if _raw_idx:
            _sf_index = _json_sf.loads(_raw_idx)
    except Exception:
        pass

    def _sf_odds_for_game(home: str, away: str) -> tuple[dict, str]:
        """Fetch Sofascore bookmaker odds for this game. Returns (odds_dict, sofascore_id).
        Tries exact match first, then fuzzy word-level match for team name variants
        (e.g. 'LA Lakers' vs 'Los Angeles Lakers') so sofascore_id is always populated.
        """
        h_low, a_low = home.lower(), away.lower()

        # 1. Exact match
        for name in (h_low, a_low):
            ev = _sf_index.get(name)
            if ev and ev.get("id"):
                try:
                    from src.apis.sofascore import get_event_odds as _gso
                    return _gso(ev["id"]) or {}, str(ev["id"])
                except Exception:
                    return {}, str(ev.get("id", ""))

        # 2. Fuzzy: check if any significant word from home/away appears in index keys
        for name in (h_low, a_low):
            words = [w for w in name.split() if len(w) > 3]
            for key, ev in _sf_index.items():
                if any(w in key for w in words) and ev.get("id"):
                    try:
                        from src.apis.sofascore import get_event_odds as _gso
                        return _gso(ev["id"]) or {}, str(ev["id"])
                    except Exception:
                        return {}, str(ev.get("id", ""))

        return {}, ""

    logger.info("HardRock [%s]: %d games in DB window", period, len(snapshots))

    _today_et = _dt.datetime.now(_zi.ZoneInfo("America/New_York")).date()

    for game_id, snap_list in list(snapshots.items())[:200]:
        if not snap_list:
            continue

        rep        = snap_list[0]
        sport_key  = rep.get("sport_key", "")
        home_team  = rep.get("home_team", "")
        away_team  = rep.get("away_team", "")
        commence   = str(rep.get("commence_time", ""))

        # ── Date / period filter ─────────────────────────────────────────────
        try:
            _ct = _dparse(commence)
            _ET_zone = _zi.ZoneInfo("America/New_York")
            if _ct.tzinfo is None:
                _ct = _ct.replace(tzinfo=_ET_zone)  # naive = ET
            _ct_et = _ct.astimezone(_ET_zone)
            if _ct_et.date() != _today_et:
                continue
            _now_et = _dt.datetime.now(_zi.ZoneInfo("America/New_York"))
            # Allow games that started within the last 3 hours — ML/spread lines
            # are still posted pre-game and props are live early in a game.
            # Exclude games finished more than 3 hours ago to avoid stale data.
            _mins_since_start = (_now_et - _ct_et).total_seconds() / 60
            if _mins_since_start > 180:
                continue  # game started 3+ hours ago — likely finished, skip
            _is_night = _ct_et.hour >= 16
            if period == "day"   and _is_night:     continue
            if period == "night" and not _is_night: continue
        except Exception:
            if sofascore_events and not (
                _team_matches(home_team, sofascore_events) or
                _team_matches(away_team, sofascore_events)
            ):
                continue

        # ── Build market groups ───────────────────────────────────────────────
        by_mkt: dict[str, list] = {}
        for s in snap_list:
            by_mkt.setdefault(s.get("market", "h2h"), []).append(s)

        game_injuries = [i for i in injuries if i.get("team") in (home_team, away_team)]
        try:
            ctx = build_game_context(sport_key=sport_key, home_team=home_team,
                                     away_team=away_team, game_time=commence)
        except Exception:
            ctx = {}
        all_injuries = ctx.get("injuries_espn_home", []) + ctx.get("rotowire_injuries", []) or game_injuries
        hub_news     = ctx.get("news_espn", [])

        # Sofascore odds — bookmaker-implied probabilities for cross-reference
        sf_odds, _sf_id = _sf_odds_for_game(home_team, away_team)
        if sf_odds:
            ctx["sofascore_odds"] = sf_odds  # picked up by analyse_pick → AI sees it

        # ── Try up to 3 markets per game ─────────────────────────────────────
        priority = ["h2h", "spreads", "totals", "team_totals", "alternate_totals",
                    "btts", "draw_no_bet", "innings_1_5_total", "team_points_q1"]
        ordered  = [m for m in priority if m in by_mkt] + [m for m in by_mkt if m not in priority]
        for mkt in ordered[:3]:
            snaps     = by_mkt[mkt]
            odds_map  = {s["book"]: s["best_odds"] for s in snaps if s.get("book")}
            if not odds_map:
                continue

            ai = analyse_pick(
                {**{"sport_key": sport_key, "home_team": home_team,
                    "away_team": away_team, "commence_time": commence}, "market": mkt},
                all_injuries, hub_news, odds_map, ctx,
            )
            if not ai:
                continue

            market    = ai.get("market", mkt)
            selection = ai.get("selection", "")
            _raw_prob = float(ai.get("win_probability", 0.5))
            opp_prob  = float(ai.get("opponent_probability") or (1 - _raw_prob))

            # Calibrate confidence via confidence engine (factors: line movement, news, sport, market)
            try:
                from src.engines.confidence_engine import compute_confidence as _cc
                _cal = _cc(
                    ai_win_prob         = _raw_prob,
                    model_consensus     = ai.get("confidence", _raw_prob),
                    line_movement_score = ctx.get("sharp_action", {}).get("score", 0.5),
                    news_impact_score   = ctx.get("news_impact_score", 0.5),
                    sport               = sport_key,
                    market              = market,
                )
                ai_prob = _cal.calibrated_score
            except Exception:
                ai_prob = _raw_prob  # fallback to raw if engine unavailable

            # Resolve odds for selected side
            books = {s["book"]: s["best_odds"] for s in snaps
                     if s.get("market") == market and s.get("selection") == selection
                     and s.get("book") and s.get("best_odds") is not None}
            if not books and selection:
                sl = selection.lower()
                books = {s["book"]: s["best_odds"] for s in snaps if s.get("book")
                         and s.get("best_odds") is not None
                         and (sl in (s.get("selection") or "").lower() or
                          (s.get("selection") or "").lower() in sl)}
            if not books:
                continue  # no matching selection odds — skip rather than use wrong team's odds

            # Best odds = highest payout: max by raw value (least negative for -odds, highest for +odds)
            best_odds = max(books.values())

            # ── Simple gate ───────────────────────────────────────────────────
            # h2h (game winner) requires full 77% — it's the hardest market to edge.
            # All markets require the same 77% floor — no exceptions
            _min_prob = CONF_FLOOR
            _top_seen_conf = max(_top_seen_conf, ai_prob)
            if best_odds > 0:
                implied = 100 / (100 + best_odds)
                if ai_prob < 0.42 or (ai_prob - implied) < 0.05:
                    logger.info("SKIP +odds [%s vs %s] %s: prob=%.0f%% implied=%.0f%%",
                                home_team, away_team, market, ai_prob*100, implied*100)
                    continue
            else:
                if ai_prob < _min_prob:
                    logger.info("SKIP -odds [%s vs %s] %s: prob=%.0f%% < %.0f%%",
                                home_team, away_team, market, ai_prob*100, _min_prob*100)
                    continue

            # Simple EV: our_prob × decimal_payout - 1 > 0
            dec = american_to_decimal(best_odds)
            ev  = ai_prob * (dec - 1) - (1 - ai_prob)
            if ev < EV_FLOOR:
                logger.info("SKIP [%s vs %s] %s: ev=%.2f%% < floor", home_team, away_team, market, ev*100)
                continue

            opp_odds = decimal_to_american(1.0 / opp_prob) if 0 < opp_prob < 1 else None
            ev_full  = evaluate(american_odds=best_odds, projected_prob=ai_prob,
                                opponent_odds=opp_odds)

            candidates.append({
                "type":         "team",
                "score":        ai_prob * (1 + ev),
                "sport_key":    sport_key,
                "home_team":    home_team,
                "away_team":    away_team,
                "commence_time":commence,
                "market":       market,
                "selection":    selection,
                "line_value":   next((s.get("line_value") for s in snaps
                                      if s.get("market") == market
                                      and s.get("selection") == selection
                                      and s.get("line_value") is not None), None),
                "best_odds":    best_odds,
                "best_book":    next((bk for bk, v in books.items() if v == best_odds), rep.get("book", "hardrock")),
                "books_odds":   books,
                "ev_pct":       ev,
                "no_vig_prob":  ev_full.no_vig_prob,
                "confidence":   ai_prob,
                "units":        ev_full.units,
                "reasoning":    ai.get("reasoning", ""),
                "key_factors":  ai.get("key_factors", []),
                "sofascore_id": _sf_id,
                "injuries":     len([i for i in all_injuries if i.get("status") in ("out", "doubtful")]),
            })

    logger.info("HardRock [%s]: %d qualified from %d games | best_conf=%.0f%%",
                period, len(candidates), len(snapshots), _top_seen_conf * 100)

    if not candidates:
        candidates.append({"_meta": True, "_top_conf": _top_seen_conf})
    else:
        candidates[0]["_top_conf"] = _top_seen_conf
    return candidates


def _build_prop_candidates(sofascore_events: list[dict]) -> list[dict]:
    """
    Build a combined pre-screen pool from ALL three prop types:
      - Player props  (player stats: points, goals, assists, etc.)
      - Team props    (team totals, team corners, team first goal, etc.)
      - Game props    (totals Over/Under, btts, alternate lines, team_totals, etc.)
    Top 5 from each type → 15 total AI analysis slots so no type gets crowded out.
    """
    from src.core.config import REDIS_URL
    from src.engines.ev_engine import implied_prob, evaluate
    from src.engines.ai_engine import analyse_pick
    from src.apis.data_hub import build_player_context
    from src.engines.odds_engine import get_latest_snapshots_by_game
    import json
    import redis as _redis

    # ── Source 1 & 2: Player props + Team props (Redis) ──────────────────────
    try:
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = r.get("props:odds_api")
        all_redis_props: list[dict] = json.loads(raw) if raw else []
    except Exception:
        all_redis_props = []

    player_pre: list[dict] = []
    team_pre:   list[dict] = []

    for prop in all_redis_props:
        player    = (prop.get("player") or prop.get("subject") or "").strip()
        stat      = prop.get("stat", "")
        sport_key = prop.get("sport_key", "")
        event_id  = prop.get("event_id", "")
        line      = prop.get("line")
        is_team   = prop.get("is_team_prop", False)
        if not stat:
            continue
        if not is_team and not player:
            continue

        over_odds  = prop.get("over_odds", {})
        under_odds = prop.get("under_odds", {})
        best_over  = max(over_odds.values(),  default=None) if over_odds  else None
        best_under = max(under_odds.values(), default=None) if under_odds else None

        # Pick the side with HIGHER implied probability (more likely = lower abs American odds)
        # implied_prob(-130) ≈ 0.565, implied_prob(+110) ≈ 0.476 → pick Over -130
        direction, best_odds_val, all_book_odds = None, None, {}
        ip_over  = implied_prob(best_over)  if best_over  is not None else 0.0
        ip_under = implied_prob(best_under) if best_under is not None else 0.0
        if best_over is not None and ip_over >= ip_under:
            direction, best_odds_val = "Over",  best_over
            all_book_odds = {f"{bk} Over": v for bk, v in over_odds.items()}
        elif best_under is not None:
            direction, best_odds_val = "Under", best_under
            all_book_odds = {f"{bk} Under": v for bk, v in under_odds.items()}

        if direction is None or best_odds_val is None:
            continue

        raw_prob = implied_prob(best_odds_val)
        if raw_prob < 0.40:  # drop extreme longshots only; let AI + EV do real gating
            continue

        opp_odds_val = (max(under_odds.values()) if direction == "Over" and under_odds
                        else max(over_odds.values()) if direction == "Under" and over_odds
                        else None)
        prop_ev = evaluate(american_odds=best_odds_val, projected_prob=raw_prob,
                           opponent_odds=opp_odds_val)

        entry = {
            "prop_type":    "team" if is_team else "player",
            "player":       player if not is_team else stat,
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
            "is_team_prop": is_team,
            "home_team":    prop.get("home_team", ""),
            "away_team":    prop.get("away_team", ""),
        }
        (team_pre if is_team else player_pre).append(entry)

    player_pre.sort(key=lambda x: x["raw_prob"], reverse=True)
    team_pre.sort(key=lambda x: x["raw_prob"], reverse=True)

    # ── Source 3: Game props (totals, btts, team_totals from DB snapshots) ────
    game_prop_markets = {
        "totals", "alternate_totals", "team_totals",
        "btts", "draw_no_bet", "double_chance",
        "h2h_corners", "h2h_cards", "innings_1_5_total",
        "team_points_q1", "team_points_q2",
    }
    game_pre: list[dict] = []
    try:
        import zoneinfo as _zi2
        from dateutil.parser import parse as _parse2
        _today_et2 = __import__('datetime').datetime.now(_zi2.ZoneInfo("America/New_York")).date()
        snapshots = get_latest_snapshots_by_game()
        for game_id, snap_list in list(snapshots.items())[:30]:
            # today-only gate — skip tomorrow's games
            _commence_chk = snap_list[0].get("commence_time", "") if snap_list else ""
            try:
                _ct2 = _parse2(_commence_chk)
                _ET_zone2 = _zi2.ZoneInfo("America/New_York")
                if _ct2.tzinfo is None:
                    _ct2 = _ct2.replace(tzinfo=_ET_zone2)  # naive = ET
                if _ct2.astimezone(_ET_zone2).date() != _today_et2:
                    continue
            except Exception:
                pass
            for snap in snap_list:
                mkt = snap.get("market", "")
                if mkt not in game_prop_markets:
                    continue
                sport_key  = snap.get("sport_key", "")
                home_team  = snap.get("home_team", "")
                away_team  = snap.get("away_team", "")
                selection  = snap.get("selection", "")
                best_odds  = snap.get("best_odds")
                if best_odds is None:
                    continue
                raw_prob = implied_prob(best_odds)
                if raw_prob < 0.40:  # drop extreme longshots only
                    continue
                prop_ev = evaluate(american_odds=best_odds, projected_prob=raw_prob)
                game_pre.append({
                    "prop_type":    "game",
                    "player":       f"{home_team} vs {away_team}",
                    "stat":         mkt,
                    "line":         selection,
                    "direction":    selection,
                    "sport_key":    sport_key,
                    "event_id":     str(game_id),
                    "best_odds":    best_odds,
                    "books_odds":   {snap.get("book", "book"): best_odds},
                    "raw_prob":     raw_prob,
                    "ev_pct":       prop_ev.ev_pct,
                    "units":        float(prop_ev.units or 1),
                    "is_team_prop": False,
                    "home_team":    home_team,
                    "away_team":    away_team,
                })
    except Exception as _ge:
        logger.warning("Game prop pre-screen failed: %s", _ge)

    game_pre.sort(key=lambda x: x["raw_prob"], reverse=True)

    # ── Merge: top 5 from each type → 15 AI slots ────────────────────────────
    pre_screened = player_pre[:5] + team_pre[:5] + game_pre[:5]
    pre_screened.sort(key=lambda x: x["raw_prob"], reverse=True)

    logger.info("Prop pre-screen: %d player, %d team, %d game → %d to AI",
                len(player_pre), len(team_pre), len(game_pre), len(pre_screened))

    # ── Phase 2: AI deep research on combined pool ────────────────────────────
    candidates: list[dict] = []
    for prop in pre_screened:
        player    = prop["player"]
        stat      = prop["stat"]
        sport_key = prop["sport_key"]
        direction = prop["direction"]
        line      = prop["line"]
        prop_type = prop.get("prop_type", "player")

        home_team = prop.get("home_team", "")
        away_team = prop.get("away_team", "")
        opponent  = away_team or home_team or ""
        commence  = prop.get("commence_time", "")
        try:
            player_ctx = build_player_context(player, sport_key, opponent=opponent, n_games=5)
        except Exception:
            player_ctx = {}

        # Merge game-level context (injuries, H2H, weather) into prop analysis
        try:
            from src.apis.data_hub import build_game_context as _bgc
            _game_ctx = _bgc(sport_key=sport_key, home_team=home_team,
                             away_team=away_team, game_time=commence) if home_team else {}
        except Exception:
            _game_ctx = {}
        merged_ctx = {**_game_ctx, **player_ctx}

        event = {
            "sport_key":      sport_key,
            "home_team":      home_team,
            "away_team":      away_team,
            "commence_time":  commence,
            "prop_player":    player,
            "prop_stat":      stat,
            "prop_line":      line,
            "prop_direction": direction,
            "prop_type":      prop_type,
            "description":    f"{player} · {stat} {direction} {line}",
        }
        ai = analyse_pick(event, [], [], {direction: prop["best_odds"]}, merged_ctx)

        if not ai:
            continue

        conf      = ai.get("win_probability", prop["raw_prob"])
        ev        = ai.get("ev_pct", prop["ev_pct"])
        reasoning = ai.get("reasoning", "")
        best_odds = prop["best_odds"]

        # Gate on numbers, not recommendation label — label is calibrated for -odds.
        # For + odds props: need 42%+ win prob AND 5%+ edge AND positive EV.
        # For - odds props: need 60%+ win prob (lower than team floor since props are noisier).
        if best_odds > 0:
            implied = 100 / (100 + best_odds)
            if conf < 0.42 or (conf - implied) < 0.05 or ev <= 0:
                continue
        else:
            if conf < 0.60 or ev <= 0:
                continue

        candidates.append({
            "type":         "prop",
            "prop_type":    prop_type,
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
            "is_team_prop": prop.get("is_team_prop", False),
            "home_team":    prop.get("home_team", ""),
            "away_team":    prop.get("away_team", ""),
            "commence_time":prop.get("commence_time", ""),
            "reasoning":    reasoning,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _detect_correlation(p1: dict, p2: dict) -> str | None:
    """
    Returns a signal label if p1 and p2 are positively correlated within the same game.
    Both must be Over bets on attack-related stats — when a team dominates,
    shots, corners, goals, and possession all move together.
    """
    stat1 = (p1.get("stat") or "").lower()
    stat2 = (p2.get("stat") or "").lower()
    dir1  = (p1.get("direction") or "").lower()
    dir2  = (p2.get("direction") or "").lower()
    pl1   = (p1.get("player") or "").lower()
    pl2   = (p2.get("player") or "").lower()

    if dir1 != "over" or dir2 != "over":
        return None  # Under vs Over negatively correlated

    attack_stats = {
        "shots", "shots on target", "corners", "goals", "assists",
        "fouls", "touches", "possession", "total shots", "offsides",
        "saves", "blocks", "hits", "strikeouts", "rushing yards",
        "receiving yards", "rebounds", "points",
    }
    is_attack1 = any(s in stat1 for s in attack_stats) or "total" in stat1
    is_attack2 = any(s in stat2 for s in attack_stats) or "total" in stat2

    if not (is_attack1 and is_attack2):
        return None

    # Same player — don't pair (e.g. shots + shots on target for same player is just one signal)
    if pl1 and pl2 and pl1 == pl2 and stat1 == stat2:
        return None

    # Same team — strongest correlation signal
    ht = (p1.get("home_team") or "").lower()
    at = (p1.get("away_team") or "").lower()
    for team in (ht, at):
        if team and (team in pl1 or pl1 in team) and (team in pl2 or pl2 in team):
            return "same_team_attack"

    # Both team-level (no specific player) — game-level attacking correlation
    if not pl1 and not pl2:
        return "game_attack_stack"

    # Mix of player + game-level over stats — high-scoring game stack
    if "total" in stat1 or "total" in stat2:
        return "high_scoring_stack"

    # Both attacking Over props in same game — correlated by match tempo
    return "game_attack_correlation"


def _build_sgp_bundles(prop_candidates: list[dict]) -> list[dict]:
    """
    Identify 2-leg correlated prop stacks from the same game.

    When a team dominates — shots, corners, goals all go up together.
    Combining them in a same-game parlay gives better value than the
    book's independent pricing implies.
    Returns a ranked list of SGP bundle dicts (type='sgp').
    """
    from itertools import combinations

    # Group props by game
    by_game: dict[str, list[dict]] = {}
    for prop in prop_candidates:
        gk = (prop.get("event_id") or
              f"{prop.get('home_team','').lower()}:{prop.get('away_team','').lower()}")
        if gk:
            by_game.setdefault(gk, []).append(prop)

    bundles: list[dict] = []
    for game_key, props in by_game.items():
        if len(props) < 2:
            continue
        for p1, p2 in combinations(props[:8], 2):
            if p1["confidence"] < 0.60 or p2["confidence"] < 0.60:
                continue
            if p1.get("stat") == p2.get("stat"):
                continue
            signal = _detect_correlation(p1, p2)
            if not signal:
                continue
            bundle_conf  = min(p1["confidence"], p2["confidence"])
            bundle_score = bundle_conf * 1.08   # correlation bonus — legs are NOT independent
            bundles.append({
                "type":         "sgp",
                "score":        bundle_score,
                "confidence":   bundle_conf,
                "sport_key":    p1.get("sport_key", ""),
                "home_team":    p1.get("home_team", ""),
                "away_team":    p1.get("away_team", ""),
                "commence_time":p1.get("commence_time", ""),
                "event_id":     game_key,
                "legs":         [p1, p2],
                "correlation":  signal,
                "ev_pct":       (p1["ev_pct"] + p2["ev_pct"]) / 2,
                "best_odds":    max(p1["best_odds"], p2["best_odds"]),  # best payout of the two legs
            })

    bundles.sort(key=lambda x: x["score"], reverse=True)
    logger.info("SGP bundles found: %d", len(bundles))
    return bundles[:3]


def _post_hardrock_embed(period: str, entry: list[dict]) -> None:
    """Build and post the HardRock Discord embed — individual picks, no parlay framing."""
    from src.core.sport_labels import get_emoji
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post
    import zoneinfo
    import hashlib
    from datetime import datetime

    ET           = zoneinfo.ZoneInfo("America/New_York")
    now_et       = datetime.now(ET)
    date_str     = now_et.strftime("%b %-d, %Y")
    period_label = "DAY ENTRY" if period == "day" else "NIGHT ENTRY"
    period_emoji = "☀️" if period == "day" else "🌙"
    _MARKET_BADGE = {
        "h2h":           "MONEYLINE",
        "spreads":       "SPREAD",
        "totals":        "TOTAL O/U",
        "alternate_totals": "ALT TOTAL",
        "team_totals":   "TEAM TOTAL",
        "btts":          "BTTS",
        "draw_no_bet":   "DNB",
        "double_chance": "DBL CHANCE",
        "h2h_corners":   "CORNERS",
        "h2h_cards":     "CARDS",
        "innings_1_5_total": "F5 TOTAL",
        "team_points_q1": "Q1 TOTAL",
        "team_points_q2": "Q2 TOTAL",
    }
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
        # Edge = how much our projected prob beats the vig-free market price
        # e.g. we say 78%, market vig-free says 42% → edge = +36pp
        _proj   = p["confidence"]
        _no_vig = p.get("no_vig_prob")
        ev      = round((_proj - _no_vig) * 100, 1) if _no_vig else round(min(p.get("ev_pct", 0), 0.25) * 100, 1)
        emoji    = get_emoji(p["sport_key"])
        odds_fmt = _fmt(p["best_odds"])
        reasoning = p.get("reasoning", "")
        reason_short = (next((reasoning[:i+1] for i in range(min(120, len(reasoning))-1, -1, -1) if reasoning[i] in ".!?"), reasoning[:120]) + ("…" if len(reasoning) > 120 else "")) if reasoning else ""

        if p["type"] == "sgp":
            gt       = _game_time(p.get("commence_time", ""))
            time_str = f"  ·  {gt}" if gt else ""
            corr_labels = {
                "same_team_attack":       "Same-team attack stack",
                "game_attack_stack":      "Game attack stack",
                "high_scoring_stack":     "High-scoring game stack",
                "game_attack_correlation":"Correlated match props",
            }
            corr_label = corr_labels.get(p.get("correlation", ""), "Correlated props")
            legs_lines = []
            for leg in p["legs"]:
                leg_dir  = (leg.get("direction") or "Over").upper()
                leg_odds = _fmt(leg.get("best_odds", ""))
                legs_lines.append(
                    f"  • **{leg.get('player') or leg.get('home_team','')}** "
                    f"— {leg.get('stat', '')} **{leg_dir} {leg.get('line','')}**  `{leg_odds}`"
                )
            pick_fields.append({
                "name":  f"PICK {i}  ·  {emoji}  `⚡ SGP — {corr_label.upper()}`",
                "value": (
                    f"**{p.get('away_team', '')} @ {p.get('home_team', '')}**{time_str}\n"
                    + "\n".join(legs_lines) + "\n"
                    f"Conf  **{conf}%**   Edge  **+{ev}%**   _(Place as Same-Game Parlay)_"
                ),
                "inline": False,
            })
        elif p["type"] == "prop":
            prop_type = p.get("prop_type", "player")
            if prop_type == "game":
                label = _MARKET_BADGE.get(p.get("stat", ""), p.get("stat", "GAME PROP").upper())
                gt    = _game_time(p.get("commence_time", ""))
                time_str = f"  ·  {gt}" if gt else ""
                pick_fields.append({
                    "name":  f"PICK {i}  ·  {emoji}  `{label}`",
                    "value": (
                        f"**{p.get('home_team', '')} vs {p.get('away_team', '')}**{time_str}\n"
                        f"✅  **{p.get('direction', '?')}**\n"
                        f"Odds  `{odds_fmt}`   Conf  **{conf}%**   Edge  **+{ev}%**"
                        + (f"\n> _{reason_short}_" if reason_short else "")
                    ),
                    "inline": False,
                })
            else:
                label    = "TEAM PROP" if p.get("is_team_prop") else "PLAYER PROP"
                direction = (p.get("direction") or "").upper()
                pick_fields.append({
                    "name":  f"PICK {i}  ·  {emoji}  `{label}`",
                    "value": (
                        f"**{p.get('player', '?')}**  —  {p.get('stat', '?')} **{direction} {p.get('line', '?')}**\n"
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
            line_val  = p.get("line_value")
            # For spreads/totals show the line next to selection
            if line_val is not None and p["market"] in ("spreads", "totals", "alternate_totals", "team_totals"):
                line_str = f" {'+' if line_val > 0 else ''}{line_val}"
            else:
                line_str = ""
            pick_fields.append({
                "name":  f"PICK {i}  ·  {emoji}  `{badge}`",
                "value": (
                    f"**{p['away_team']} @ {p['home_team']}**{inj}{time_str}\n"
                    f"✅  **{p['selection']}{line_str}**\n"
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
    Ranks by confidence score, takes exactly 2 picks (hard cap).
    Posts to Discord only if 2 qualifying picks exist.
    """
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}

    # HardRock entries paused until July 10, 2026
    from datetime import date as _date_cls
    from src.core.timezone import et_naive as _et_naive_hr
    if _et_naive_hr().date() < _date_cls(2026, 7, 10):
        logger.info("HardRock %s entry paused until July 10, 2026", period)
        return {"skipped": "paused_until_july_10", "period": period}

    # Skip entirely if Odds API credits are exhausted — no data to work with
    try:
        from src.engines.odds_engine import _credits_exhausted
        if _credits_exhausted():
            logger.info("HardRock %s entry: Odds API credits exhausted — skipping until July 10", period)
            return {"skipped": "odds_api_exhausted", "period": period}
    except Exception:
        pass

    # Dedup: only post once per period per day
    _r_dedup = None
    _dedup_key = None
    try:
        import redis as _redis
        from src.core.config import REDIS_URL
        from src.core.timezone import et_naive as _et_naive
        _r_dedup = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _today = _et_naive().strftime("%Y-%m-%d")
        _dedup_key = f"hardrock:posted:{period}:{_today}"
        # Atomic SET NX — only one process wins even on simultaneous restart
        if not _r_dedup.set(_dedup_key, "1", ex=3600, nx=True):
            logger.info("HardRock %s entry already posted today — skipping duplicate", period)
            return {"skipped": "already_posted", "period": period}
    except Exception:
        pass  # Redis unavailable — allow through

    try:
        sofascore_events = _load_todays_games(period)
        if not sofascore_events:
            logger.info("HardRock %s entry: no Sofascore cache yet — proceeding anyway", period)

        # If DB has no snapshots at all (e.g. runner just restarted before first scan),
        # run one emergency scan synchronously so we have data to work with.
        from src.engines.odds_engine import get_latest_snapshots_by_game as _check_snaps
        if not _check_snaps():
            logger.warning("HardRock %s: no snapshots in DB — running emergency odds scan", period)
            try:
                from src.workers.odds_worker import scan_and_save_odds as _emergency_scan
                _emergency_scan()
            except Exception as _se:
                logger.warning("Emergency odds scan failed: %s", _se)

        _game_raw  = _build_hardrock_candidates(period, sofascore_events)
        _props_raw = _build_prop_candidates(sofascore_events)
        _top_conf_seen = max(
            next((c.get("_top_conf", 0) for c in _game_raw  if c.get("_meta")), 0),
            next((c.get("_top_conf", 0) for c in _props_raw if c.get("_meta")), 0),
            max((c.get("confidence", 0) for c in _game_raw  if not c.get("_meta")), default=0),
            max((c.get("confidence", 0) for c in _props_raw if not c.get("_meta")), default=0),
        )
        raw_game  = [c for c in _game_raw  if not c.get("_meta")]
        raw_props = [c for c in _props_raw if not c.get("_meta")]
        # Build SGP bundles from qualified props — correlated stacks rank above individual picks
        sgp_bundles = _build_sgp_bundles(raw_props)
        pool = sorted(raw_game + raw_props + sgp_bundles, key=lambda x: x["score"], reverse=True)

        # Every pick must independently justify its inclusion.
        # For a parlay: combined win probability × combined payout must be > 1 (positive EV).
        # If adding a second leg makes the parlay EV negative, post the single instead.

        def _american_to_dec(odds: int) -> float:
            return (odds / 100 + 1) if odds > 0 else (100 / abs(odds) + 1)

        def _parlay_is_profitable(picks: list[dict]) -> bool:
            """Return True only if combined_win_prob × parlay_decimal_payout > 1."""
            combined_win_prob = 1.0
            combined_dec      = 1.0
            for p in picks:
                combined_win_prob *= p["confidence"]
                combined_dec      *= _american_to_dec(round(p["best_odds"]))
            return combined_win_prob * combined_dec > 1.0

        # Final gate: enforce EV ≥ 3%, reasoning ≥ 80 chars, ≥ 2 key factors.
        # This mirrors pick_gate.check() thresholds for dict-based candidates.
        _MIN_EV_GATE = 0.03
        _MIN_REASON  = 80
        _MIN_FACTORS = 2
        _gated_pool  = []
        for _p in pool:
            _ev  = _p.get("ev_pct", 0)
            _rsn = (_p.get("reasoning") or "").strip()
            _fct = [f for f in (_p.get("key_factors") or []) if f and str(f).strip()]
            if _ev < _MIN_EV_GATE:
                logger.info("GATE BLOCK EV [%s %s]: ev=%.2f%% < 3%%",
                            _p.get("home_team"), _p.get("away_team"), _ev * 100)
                continue
            if len(_rsn) < _MIN_REASON:
                logger.info("GATE BLOCK REASONING [%s %s]: %d chars < %d",
                            _p.get("home_team"), _p.get("away_team"), len(_rsn), _MIN_REASON)
                continue
            if len(_fct) < _MIN_FACTORS:
                logger.info("GATE BLOCK FACTORS [%s %s]: %d < %d",
                            _p.get("home_team"), _p.get("away_team"), len(_fct), _MIN_FACTORS)
                continue
            _gated_pool.append(_p)
        pool = _gated_pool

        # Favorite-first: -odds picks always rank ahead of +odds picks.
        # +odds underdogs only make the slip when no -odds alternative exists.
        _favs    = [p for p in pool if p.get("best_odds", 0) < 0]
        _dogs    = [p for p in pool if p.get("best_odds", 0) >= 0]
        pool     = _favs + _dogs  # favorites first, underdogs fill remaining slots

        entry: list[dict]            = []
        blocked_event_keys: set[str] = set()
        seen_players: set[str]       = set()

        for pick in pool:
            if len(entry) == 2:
                break

            if pick["type"] == "sgp":
                event_key = str(pick.get("event_id", ""))
                if event_key and event_key in blocked_event_keys:
                    continue
                if event_key:
                    blocked_event_keys.add(event_key)
            elif pick["type"] == "prop":
                player_key = (pick.get("player") or "").lower()
                event_key  = str(pick.get("event_id", ""))
                if event_key and event_key in blocked_event_keys:
                    continue
                if player_key and player_key in seen_players:
                    continue
                seen_players.add(player_key)
            else:
                event_key = f"{pick.get('home_team','')}:{pick.get('away_team','')}".lower()
                if event_key in blocked_event_keys:
                    continue
                blocked_event_keys.add(event_key)

            entry.append(pick)

        if len(entry) < 1:
            # ── LAST RESORT: Perplexity web search ───────────────────────────
            # Only fires when everything else (Sofascore + Sportradar + ESPN +
            # Action Network) was not enough. Takes the top 2 closest candidates
            # and gives them one final chance with fresh web research.
            best_conf = round(max(
                max((p["confidence"] for p in pool), default=0),
                _top_conf_seen,
            ) * 100)
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
                if 0.70 <= p["confidence"] < CONF_FLOOR and p.get("ev_pct", 0) >= EV_FLOOR
            ][:1]  # one candidate only — absolute last resort

            # Daily rate gate — max 2 Perplexity calls per day across all entries
            _perplexity_allowed = False
            try:
                from src.core.config import REDIS_URL, PERPLEXITY_API_KEY
                import redis as _redis
                from datetime import datetime as _dt
                if PERPLEXITY_API_KEY and close_candidates:
                    _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
                    from src.core.timezone import et_naive as _et_naive
                    _date_key = f"perplexity:calls:{_et_naive().strftime('%Y-%m-%d')}"
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
                            if not ai2:
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
                            if not ai2:
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
                logger.info("HardRock %s: no qualifying picks after all research (best_conf=%.1f%%)", period, best_conf)
                # Release dedup lock so the next runner tick can retry
                try:
                    if _r_dedup and _dedup_key:
                        _r_dedup.delete(_dedup_key)
                except Exception:
                    pass
                return {"picks": 0, "period": period, "posted": False}

        if len(entry) < 2:
            logger.info("HardRock %s: only %d qualifying pick(s) — need 2 to post", period, len(entry))
            try:
                if _r_dedup and _dedup_key:
                    _r_dedup.delete(_dedup_key)  # release lock so next scan can retry
            except Exception:
                pass
            return {"picks": len(entry), "period": period, "posted": False, "reason": "need_2_picks"}

        _post_hardrock_embed(period, entry)

        try:
            import hashlib as _hl
            import zoneinfo as _zi
            from datetime import datetime as _dt2
            _date_str = _dt2.now(_zi.ZoneInfo("America/New_York")).strftime("%b %-d, %Y")
            _slip_ticket = _hl.md5(f"{period}{_date_str}".encode()).hexdigest()[:8].upper()
            from src.workers.slip_tracker import save_slip
            save_slip(period, "hardrock", entry, ticket_id=_slip_ticket)
        except Exception as e:
            logger.warning("slip_tracker.save_slip failed: %s", e)

        # Extend dedup key to full 24h now that posting succeeded
        try:
            if _r_dedup and _dedup_key:
                _r_dedup.expire(_dedup_key, 86400)
        except Exception:
            pass

        return {"period": period, "picks": len(entry), "posted": True}

    except Exception as exc:
        logger.error("HardRock %s entry failed: %s", period, exc)
        # Release dedup key so the entry can be retried
        try:
            if _r_dedup and _dedup_key:
                _r_dedup.delete(_dedup_key)
        except Exception:
            pass
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
        from datetime import timedelta
        from src.core.timezone import et_naive

        # Extract plain values inside session — avoids DetachedInstanceError after close
        with get_db() as db:
            rows = db.query(
                Pick.id, Pick.game_id, Pick.selection, Pick.sport,
                Pick.market, Pick.best_book, Pick.american_odds_at_gen,
                Pick.confidence_pct, Pick.ev_pct,
            ).filter(
                Pick.generated_at >= et_naive() - timedelta(hours=12),
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


