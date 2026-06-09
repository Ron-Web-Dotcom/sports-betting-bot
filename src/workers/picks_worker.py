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
    return 3 <= et.hour < 5


def generate_picks():
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}
    try:
        # Idempotency guard — prevent duplicate runs within the same 10-min window.
        # Uses Redis SETNX so only the first concurrent invocation proceeds.
        from src.core.config import REDIS_URL
        import redis as _redis
        from datetime import datetime
        _r = _redis.from_url(REDIS_URL, socket_connect_timeout=2, decode_responses=True)
        _lock_key = f"pick_gen_lock:{datetime.utcnow().strftime('%Y%m%d%H')}"
        if not _r.set(_lock_key, "1", nx=True, ex=650):
            logger.info("generate_picks: another instance already running, skipping")
            return {"skipped": True}
    except Exception as _lock_exc:
        logger.warning("Redis lock unavailable (%s) — proceeding without idempotency guard", _lock_exc)

    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.engines.news_engine import get_recent_injuries
        from src.engines.confidence_engine import compute_confidence
        from src.engines.risk_engine import assess
        from src.engines.comparison_engine import compare_all_markets
        from src.engines.ai_engine import analyse_pick
        from src.engines.recommendation_engine import build_recommendation, persist_pick

        snapshots = get_latest_snapshots_by_game()
        injuries = get_recent_injuries()
        bet_picks = []

        for game_id, snap_list in snapshots.items():
            if not snap_list:
                continue

            best_snap = snap_list[0]
            event = {
                "sport_key": best_snap.get("sport_key", ""),
                "home_team": best_snap.get("home_team", ""),
                "away_team": best_snap.get("away_team", ""),
                "commence_time": str(best_snap.get("commence_time", "")),
            }
            game_injuries = [i for i in injuries if
                             i.get("team") in (event["home_team"], event["away_team"])]

            odds_by_book = {s["book"]: s["best_odds"] for s in snap_list if "book" in s}

            # Build full real-world context from all data sources
            from src.apis.data_hub import build_game_context
            game_context = build_game_context(
                sport_key  = event["sport_key"],
                home_team  = event["home_team"],
                away_team  = event["away_team"],
                game_time  = event["commence_time"],
            )

            # Merge hub injuries (richer) with odds-engine injuries
            hub_injuries = (
                game_context.get("injuries_espn_home", []) +
                game_context.get("rotowire_injuries", [])
            )
            all_injuries = hub_injuries or game_injuries

            hub_news = game_context.get("news_espn", [])

            ai = analyse_pick(event, all_injuries, hub_news, odds_by_book, game_context)
            if not ai:
                logger.warning("AI analysis returned None for game_id=%s — skipping", game_id)
                continue

            from src.engines.ev_engine import american_to_decimal, implied_prob, remove_vig, evaluate
            best_odds = best_snap.get("best_odds", -110)

            # Use the full evaluate() pipeline for a complete EVResult.
            # opponent_odds must be the OTHER side of the market (for vig removal),
            # not a second book's price for the same selection.
            # The AI returns the opposing side's implied probability; convert back to american.
            opp_prob = ai.get("opponent_probability")
            if opp_prob is not None and 0 < opp_prob < 1:
                from src.engines.ev_engine import decimal_to_american
                opp_decimal = 1.0 / opp_prob
                opponent_odds = decimal_to_american(opp_decimal)
            else:
                opponent_odds = None
            ev_result = evaluate(
                american_odds   = best_odds,
                projected_prob  = ai.get("win_probability", 0.5),
                opponent_odds   = opponent_odds,
            )

            confidence = compute_confidence(
                ai_win_prob         = ai.get("win_probability", 0.5),
                model_consensus     = ai.get("confidence", 0.5),
                line_movement_score = game_context.get("sharp_action", {}).get("score", 0.5),
                news_impact_score   = game_context.get("news_impact_score", 0.5),
                sport               = event["sport_key"],
                market              = "h2h",
            )

            # Adjust confidence down when data is incomplete
            data_quality = game_context.get("data_completeness", 1.0)
            if data_quality < 0.5:
                confidence.calibrated_score = round(confidence.calibrated_score * data_quality, 4)

            risk = assess(
                requested_units  = ev_result.units,
                sport            = event["sport_key"],
                ev_pct           = ev_result.ev_pct,
                confidence       = confidence.calibrated_score,
                win_prob         = ev_result.projected_prob,
                decimal_odds     = ev_result.decimal_odds,
                injury_flags     = sum(1 for i in all_injuries if i.get("status") in ("out", "doubtful")),
            )

            # compare_all_markets expects {markets: {market: {selection: [book_entries]}}}
            # Build that structure from the flat snap_list
            from src.engines.ev_engine import american_to_decimal, implied_prob as _ip  # noqa: F811
            markets_dict: dict = {}
            for s in snap_list:
                mkt, sel, book = s.get("market","h2h"), s.get("selection",""), s.get("book","")
                odds = s.get("best_odds", -110)
                markets_dict.setdefault(mkt, {}).setdefault(sel, []).append({
                    "book": book,
                    "american_odds": odds,
                    "decimal_odds": american_to_decimal(odds),
                    "implied_prob": _ip(odds),
                })
            comparison = compare_all_markets({"markets": markets_dict})

            pick = build_recommendation(
                sport=event["sport_key"],
                game=f"{event['home_team']} vs {event['away_team']}",
                bet=ai.get("selection", ""),
                market=ai.get("market", best_snap.get("market", "h2h")),
                ev_result=ev_result,
                confidence=confidence,
                risk=risk,
                comparison=comparison,
                ai_reasoning=ai.get("reasoning", ""),
                key_factors=ai.get("key_factors", []),
                statistical_score=ai.get("statistical_score", 0.5),
                ml_score=ai.get("confidence", 0.5),
                market_score=ai.get("market_score", 0.5),
                trend_score=ai.get("trend_score", 0.5),
            )

            pick_id = persist_pick(pick, game_id, odds_by_book=odds_by_book)

            # pick_id is None when the gate blocked the pick — do not alert
            if pick.recommendation == "BET" and pick_id is not None:
                import dataclasses
                pick_dict = dataclasses.asdict(pick)
                pick_dict["id"] = pick_id
                bet_picks.append(pick_dict)

        if bet_picks:
            from src.workers.alert_worker import send_pick_alerts
            send_pick_alerts(bet_picks)

        logger.info("Pick generation complete: %d BET picks", len(bet_picks))
        return {"total_games": len(snapshots), "bet_picks": len(bet_picks)}

    except Exception as exc:
        logger.error("Pick generation failed: %s", exc)
        raise


def morning_props_brief():
    """
    8 AM Eastern — PP and HardRock lines are now live.
    Force-fetch fresh props, run AI picks, post a full morning brief:
      • Today's games
      • Which props to bet (Over/Under + reasoning)
      • HardRock parlay suggestions from the Odds API
    """
    import json, dataclasses
    from src.workers.alert_worker import _run_async
    from src.discord_bot.bot import _post, _embed
    from datetime import datetime
    import zoneinfo

    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # ── 1. Force-refresh Odds API player props (lines just opened) ───────────
    try:
        from src.engines.odds_engine import fetch_all_player_props, scan_all_sports
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        all_events = scan_all_sports()
        all_props  = fetch_all_player_props(all_events)
        r.setex("props:odds_api", 1500, json.dumps(all_props))
        r.setex("props:all",      1500, json.dumps(all_props))
    except Exception as e:
        logger.warning("Morning props fetch failed: %s", e)
        all_props = []

    # ── 2. AI picks on fresh props ────────────────────────────────────────────
    picks = []
    if all_props:
        try:
            from src.engines.prop_engine import score_props
            picks, _ = score_props(all_props)
        except Exception as e:
            logger.warning("Morning prop scoring failed: %s", e)

    # ── 3. HardRock parlay suggestions (top EV combos from Odds API) ──────────
    parlay_lines: list[str] = []
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        snaps = get_latest_snapshots_by_game()
        top_games = []
        for gid, snap_list in list(snaps.items())[:5]:
            if snap_list:
                s = snap_list[0]
                top_games.append(
                    f"{s.get('away_team','?')} @ {s.get('home_team','?')} "
                    f"({s.get('sport_key','').split('_')[-1].upper()})"
                )
        parlay_lines = top_games
    except Exception:
        pass

    # ── 4. Build Discord message ──────────────────────────────────────────────
    embeds = []

    # Morning overview
    prop_summary = f"**{len(picks)} prop picks** ready" if picks else "No high-confidence props yet — lines may still be loading"
    parlay_text  = "\n".join(f"• {g}" for g in parlay_lines) if parlay_lines else "—"

    embeds.append(_embed(
        title=f"🌅 8 AM Brief — {et.strftime('%A, %B %-d')}",
        description=(
            f"Lines are live. Here's your morning betting brief.\n\n"
            f"📋 {prop_summary}\n"
            f"🔗 HardRock parlay candidates:\n{parlay_text}"
        ),
        color=0x2E7D32,
        fields=[
            {"name": "Sources Checked", "value": "Odds API · HardRock · Kalshi · Polymarket", "inline": False},
        ],
    ))

    # Top prop picks (max 10 in one message)
    if picks:
        pick_lines = []
        for p in picks[:10]:
            direction = "📈 OVER" if p.direction == "over" else "📉 UNDER"
            sport = p.sport_key.split("_")[-1].upper()
            pick_lines.append(
                f"**{p.subject}** {p.stat} {p.line} → {direction} "
                f"({sport} | {p.confidence*100:.0f}% conf | +{p.ev_pct*100:.1f}% edge)"
            )
        embeds.append(_embed(
            title="🎯 Today's Prop Picks",
            description="\n".join(pick_lines),
            color=0x1565C0,
            fields=[
                {"name": "⚠️ Reminder", "value": "These are recommendations only. Bet responsibly.", "inline": False},
            ],
        ))

        # Parlay bundles — HardRock only
        from src.workers.alert_worker import send_hardrock_parlay_alert
        pick_dicts = [dataclasses.asdict(p) for p in picks]
        _post_parlay_bundles(pick_dicts, send_hardrock_parlay_alert)

    _run_async(_post({"embeds": embeds}))
    logger.info("Morning props brief sent: %d picks at 8 AM ET", len(picks))
    return {"picks": len(picks), "props_fetched": len(all_props)}


def scan_and_pick_props():
    """
    Prop pick cycle — runs every 5 min (skips during sleep).
    Posts ONE summary embed to Discord with the top picks, no spamming.
    Only posts if picks changed since last scan.
    """
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        import json, dataclasses, hashlib
        from datetime import datetime, timezone

        if _is_sleep_time():
            return {"skipped": "sleep_mode"}

        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        raw = r.get("props:all")
        if not raw:
            logger.info("scan_and_pick_props: no props data in cache yet")
            return {"picks": 0}

        props = json.loads(raw)
        if not props:
            return {"picks": 0}

        # Filter: no completed games, no blank subject names
        props = [p for p in props if p.get("status", "").lower() not in ("final", "completed", "in progress")]
        props = [p for p in props if p.get("subject", "").strip()]

        # Filter: only include sports that actually have props live right now
        # (auto-detects active seasons — no hardcoded months needed)
        active_sports = {p.get("sport_key") for p in props if p.get("sport_key")}
        # Remove sports with fewer than 5 props (likely stale/off-season leftovers)
        sport_counts = {}
        for p in props:
            sk = p.get("sport_key", "")
            sport_counts[sk] = sport_counts.get(sk, 0) + 1
        active_sports = {sk for sk, cnt in sport_counts.items() if cnt >= 5}
        props = [p for p in props if p.get("sport_key") in active_sports]
        logger.info("Active sports with props: %s", sorted(active_sports))

        # Filter: games starting 30 min from now up to 6 hours away, today ET only
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        from dateutil.parser import parse as _parse
        import zoneinfo
        et_now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

        def _is_upcoming_today(p: dict) -> bool:
            gt = p.get("game_time", "")
            if not gt:
                return True  # include props with no explicit time
            try:
                t = _parse(gt)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                too_soon   = t < now + timedelta(minutes=30)   # game starts in <30 min or already started
                too_far    = t > now + timedelta(hours=6)       # game more than 6 hrs away
                t_et       = t.astimezone(zoneinfo.ZoneInfo("America/New_York"))
                wrong_day  = t_et.date() != et_now.date()
                return not too_soon and not too_far and not wrong_day
            except Exception:
                return True
        props = [p for p in props if _is_upcoming_today(p)]

        from src.engines.prop_engine import score_props
        picks, watchlist = score_props(props)

        # Line shop disabled — PrizePicks and Underdog removed

        if not picks:
            # Still post watchlist if there are near-misses worth watching
            if watchlist:
                watchlist_dicts = [dataclasses.asdict(p) for p in watchlist[:8]]
                watchlist_hash = hashlib.md5(json.dumps(watchlist_dicts, sort_keys=True).encode()).hexdigest()
                if r.get("props:last_watchlist_hash") != watchlist_hash:
                    r.setex("props:last_watchlist_hash", 3600, watchlist_hash)
                    from src.workers.alert_worker import send_watchlist_update
                    send_watchlist_update(watchlist_dicts)
            return {"props_analysed": len(props), "picks": 0}

        # Top 5 only
        picks = picks[:5]

        # Only post to Discord if picks changed since last scan
        pick_dicts = [dataclasses.asdict(p) for p in picks]
        picks_hash = hashlib.md5(json.dumps(pick_dicts, sort_keys=True).encode()).hexdigest()
        last_hash = r.get("props:last_picks_hash")
        picks_changed = picks_hash != last_hash
        if picks_changed:
            r.setex("props:last_picks_hash", 3600, picks_hash)
            # Store active picks so odds_worker can track line moves on them
            r.setex("props:active_picks", 3600, json.dumps(pick_dicts))

            from src.workers.alert_worker import send_prop_summary, send_hardrock_entry, send_hardrock_parlay_alert

            # Post A: Top Player Prop Picks summary
            send_prop_summary(pick_dicts)

            # Post B: HardRock Entry — top game picks from Odds API (DB) or props cache
            try:
                from src.workers.alert_worker import send_hardrock_entry
                hr_games = []

                # Try DB snapshots first
                try:
                    from src.engines.odds_engine import get_latest_snapshots_by_game
                    snaps = get_latest_snapshots_by_game()
                    for gid, snap_list in list(snaps.items())[:8]:
                        if not snap_list:
                            continue
                        s = snap_list[0]
                        # Build per-book odds comparison
                        books_odds = {
                            snap.get("book", "unknown"): snap.get("best_odds", -110)
                            for snap in snap_list if snap.get("book")
                        }
                        hr_games.append({
                            "home_team":     s.get("home_team", ""),
                            "away_team":     s.get("away_team", ""),
                            "sport_key":     s.get("sport_key", ""),
                            "commence_time": str(s.get("commence_time", "")),
                            "best_odds":     s.get("best_odds", -110),
                            "book":          s.get("book", "HardRock"),
                            "market":        s.get("market", "h2h"),
                            "selection":     s.get("selection", ""),
                            "books_odds":    books_odds,
                        })
                except Exception:
                    pass

                # Fallback: build from today's prop picks (shows matchups even without odds)
                if not hr_games and pick_dicts:
                    seen = set()
                    for p in pick_dicts:
                        if not p.get("opponent") or not p.get("team"):
                            continue
                        key = f"{p.get('team')}|{p.get('opponent')}"
                        if key in seen:
                            continue
                        seen.add(key)
                        hr_games.append({
                            "home_team":     p.get("team", ""),
                            "away_team":     p.get("opponent", ""),
                            "sport_key":     p.get("sport_key", ""),
                            "commence_time": p.get("game_time", ""),
                            "best_odds":     -110,
                            "book":          "HardRock",
                            "market":        "h2h",
                            "selection":     p.get("team", ""),
                        })

                if hr_games:
                    send_hardrock_entry(hr_games[:4])
            except Exception as _hre:
                logger.debug("HardRock entry failed: %s", _hre)

            # Post E: Kalshi Entry — AI-scored sports prediction markets
            try:
                kalshi_raw = r.get("kalshi:markets")
                if kalshi_raw:
                    kalshi_markets_data = json.loads(kalshi_raw)
                    if kalshi_markets_data:
                        from src.workers.alert_worker import send_kalshi_entry
                        # AI score the markets before posting
                        scored = _score_kalshi_markets(kalshi_markets_data)
                        if scored:
                            scored_hash = hashlib.md5(
                                json.dumps(scored, sort_keys=True).encode()
                            ).hexdigest()
                            if r.get("kalshi:last_hash") != scored_hash:
                                r.setex("kalshi:last_hash", 3600, scored_hash)
                                send_kalshi_entry(scored)
            except Exception as _ke:
                logger.debug("Kalshi entry failed: %s", _ke)

            # Post F: Polymarket Entry — AI-scored sports prediction markets
            try:
                poly_raw = r.get("polymarket:markets")
                if poly_raw:
                    poly_data = json.loads(poly_raw)
                    if poly_data:
                        from src.workers.alert_worker import send_polymarket_entry
                        scored_poly = _score_kalshi_markets(poly_data)  # same AI scoring logic
                        if scored_poly:
                            poly_hash = hashlib.md5(
                                json.dumps(scored_poly, sort_keys=True).encode()
                            ).hexdigest()
                            if r.get("polymarket:last_hash") != poly_hash:
                                r.setex("polymarket:last_hash", 3600, poly_hash)
                                send_polymarket_entry(scored_poly)
            except Exception as _pe:
                logger.debug("Polymarket entry failed: %s", _pe)

            logger.info("Prop picks posted: %d picks", len(picks))
        else:
            logger.info("Prop picks unchanged — skipping Discord post")

        # Post watchlist if it changed — stable hash (round conf to 1dp to ignore noise)
        if watchlist:
            watchlist_dicts = [dataclasses.asdict(p) for p in watchlist[:8]]
            # Round floats so tiny AI score shifts don't trigger a new post
            stable = [
                {**w, "confidence": round(w.get("confidence", 0), 1),
                       "ev_pct":    round(w.get("ev_pct", 0), 2)}
                for w in watchlist_dicts
            ]
            watchlist_hash = hashlib.md5(json.dumps(stable, sort_keys=True).encode()).hexdigest()
            # 30-min cooldown — don't re-post the same watchlist every 5 min
            if r.get("props:last_watchlist_hash") != watchlist_hash:
                r.setex("props:last_watchlist_hash", 1800, watchlist_hash)
                from src.workers.alert_worker import send_watchlist_update
                send_watchlist_update(watchlist_dicts)

        return {"props_analysed": len(props), "picks": len(picks), "posted": picks_changed}

    except Exception as exc:
        logger.error("scan_and_pick_props failed: %s", exc)
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
    from src.workers.alert_worker import send_pp_parlay_alert, send_hardrock_parlay_alert
    return send_pp_parlay_alert, send_hardrock_parlay_alert


def _get_todays_games_from_sofascore() -> set[str]:
    """
    Ask Sofascore which games are scheduled or live today (ET).
    Returns a set of lowercase team-name tokens so we can match snapshot entries.
    """
    from src.apis.sofascore import SPORT_MAP, get_scheduled_events, get_live_events
    from src.core.timezone import et_naive
    from concurrent.futures import ThreadPoolExecutor

    today = et_naive().strftime("%Y-%m-%d")
    tokens: set[str] = set()

    def _fetch(sport_key: str):
        out = []
        try:
            out.extend(get_scheduled_events(sport_key, today))
        except Exception:
            pass
        try:
            out.extend(get_live_events(sport_key))
        except Exception:
            pass
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch, sk) for sk in SPORT_MAP]
        for fut in futures:
            for ev in fut.result():
                tokens.add(ev.get("home_team", "").lower())
                tokens.add(ev.get("away_team", "").lower())

    logger.info("Sofascore today's games: %d team tokens", len(tokens))
    return tokens


def _team_in_today(team: str, today_tokens: set[str]) -> bool:
    """Fuzzy match: a snapshot team name is in today's Sofascore game list."""
    tl = team.lower()
    if tl in today_tokens:
        return True
    # substring match for abbreviations / slight name differences
    return any(tl in tok or tok in tl for tok in today_tokens if tok)


def generate_hardrock_entry():
    """
    Build a deep-research HardRock Bet entry — 1 to 10 picks.

    Only considers games Sofascore confirms are scheduled or live today (ET).
    Games can be at any hour — morning, afternoon, night — all are included.
    For each qualifying game:
      - Pulls ML, spread, total odds across all books
      - Runs EV engine, confidence engine, AI analysis
      - Factors in injuries, recent form, H2H, sharp money
      - Ranks picks by confidence × EV
      - Posts top 1-10 picks as a single HardRock entry card to Discord
    """
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}
    try:
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.engines.news_engine import get_recent_injuries
        from src.engines.confidence_engine import compute_confidence
        from src.engines.ev_engine import evaluate, american_to_decimal, implied_prob, decimal_to_american
        from src.engines.ai_engine import analyse_pick
        from src.apis.data_hub import build_game_context
        from src.core.sport_labels import get_emoji, get_name
        from src.workers.alert_worker import _run_async
        from src.discord_bot.bot import _post
        import dataclasses, zoneinfo
        from datetime import datetime

        # Gate: only games Sofascore says are on today
        today_tokens = _get_todays_games_from_sofascore()

        snapshots  = get_latest_snapshots_by_game()
        injuries   = get_recent_injuries()
        candidates = []

        for game_id, snap_list in snapshots.items():
            if not snap_list:
                continue

            best_snap  = snap_list[0]
            sport_key  = best_snap.get("sport_key", "")
            home_team  = best_snap.get("home_team", "")
            away_team  = best_snap.get("away_team", "")
            commence   = str(best_snap.get("commence_time", ""))

            # Skip if Sofascore doesn't confirm this game is today
            if today_tokens and not (
                _team_in_today(home_team, today_tokens) or
                _team_in_today(away_team, today_tokens)
            ):
                logger.debug("HardRock entry: skipping %s @ %s — not in today's Sofascore schedule", away_team, home_team)
                continue

            event = {
                "sport_key":     sport_key,
                "home_team":     home_team,
                "away_team":     away_team,
                "commence_time": commence,
            }

            game_injuries = [i for i in injuries if i.get("team") in (home_team, away_team)]
            odds_by_book  = {s["book"]: s["best_odds"] for s in snap_list if "book" in s}

            try:
                game_context = build_game_context(
                    sport_key=sport_key, home_team=home_team,
                    away_team=away_team, game_time=commence,
                )
            except Exception:
                game_context = {}

            hub_injuries = (
                game_context.get("injuries_espn_home", []) +
                game_context.get("rotowire_injuries", [])
            )
            all_injuries = hub_injuries or game_injuries
            hub_news     = game_context.get("news_espn", [])

            ai = analyse_pick(event, all_injuries, hub_news, odds_by_book, game_context)
            if not ai:
                continue

            best_odds_val = best_snap.get("best_odds", -110)
            opp_prob      = ai.get("opponent_probability")
            opponent_odds = None
            if opp_prob and 0 < opp_prob < 1:
                opponent_odds = decimal_to_american(1.0 / opp_prob)

            ev_result  = evaluate(
                american_odds  = best_odds_val,
                projected_prob = ai.get("win_probability", 0.5),
                opponent_odds  = opponent_odds,
            )
            confidence = compute_confidence(
                ai_win_prob         = ai.get("win_probability", 0.5),
                model_consensus     = ai.get("confidence", 0.5),
                line_movement_score = game_context.get("sharp_action", {}).get("score", 0.5),
                news_impact_score   = game_context.get("news_impact_score", 0.5),
                sport               = sport_key,
                market              = "h2h",
            )

            # Only include genuine edges
            if confidence.calibrated_score < 0.55 or ev_result.ev_pct < 0.01:
                continue

            # Build per-book odds map for this pick
            market    = ai.get("market", best_snap.get("market", "h2h"))
            selection = ai.get("selection", "")
            books_odds = {
                s["book"]: s["best_odds"]
                for s in snap_list
                if s.get("market") == market and s.get("selection") == selection and s.get("book")
            }
            if not books_odds:
                books_odds = odds_by_book

            # Score = confidence × (1 + ev_pct) — ranks by both factors
            score = confidence.calibrated_score * (1 + ev_result.ev_pct)

            candidates.append({
                "score":        score,
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
                "injuries":     len([i for i in all_injuries if i.get("status") in ("out","doubtful")]),
            })

        if not candidates:
            logger.info("HardRock entry: no qualifying picks found")
            return {"picks": 0}

        # Sort by score, take top 10
        candidates.sort(key=lambda x: x["score"], reverse=True)
        picks = candidates[:10]

        # ── Build Discord embed ───────────────────────────────────────────────
        _MARKET_LABEL = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
        now_et   = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
        now_str  = now_et.strftime("%I:%M %p ET")
        date_str = now_et.strftime("%A, %B %-d")

        def _fmt_odds(v) -> str:
            return f"+{v}" if isinstance(v, (int, float)) and v > 0 else str(v)

        leg_lines = []
        for i, p in enumerate(picks, 1):
            sport    = p["sport_key"]
            emoji    = get_emoji(sport)
            league   = get_name(sport)
            market   = _MARKET_LABEL.get(p["market"], p["market"].title())
            conf     = round(p["confidence"] * 100)
            ev       = round(p["ev_pct"] * 100, 1)
            units    = round(p["units"], 1)
            odds_str = _fmt_odds(p["best_odds"])

            # Game time
            time_str = ""
            try:
                from dateutil.parser import parse as _parse
                t = _parse(p["commence_time"]).astimezone(zoneinfo.ZoneInfo("America/New_York"))
                time_str = t.strftime("%-I:%M %p ET")
            except Exception:
                pass

            # Book comparison — best book bolded, sorted best→worst
            best_book = (p.get("best_book") or "").lower()
            book_parts = []
            for bk, bk_odds in sorted(p["books_odds"].items(), key=lambda x: -(x[1] if isinstance(x[1], (int, float)) else -9999)):
                tag = f"**{bk.upper()}** {_fmt_odds(bk_odds)}" if bk.lower() == best_book else f"{bk.upper()} {_fmt_odds(bk_odds)}"
                book_parts.append(tag)
            books_line = " · ".join(book_parts[:5]) if book_parts else "—"

            # Key insight — first factor or first sentence of reasoning
            factors   = p.get("key_factors") or []
            reasoning = (p.get("reasoning") or "").strip()
            insight   = factors[0] if factors else (reasoning.split(".")[0][:100] if reasoning else "")
            inj_note  = "  ⚠️ injuries" if p["injuries"] > 0 else ""

            leg_lines.append(
                f"**{i}. {emoji} {p['away_team']} @ {p['home_team']}**  {time_str}{inj_note}\n"
                f"> {league}  ·  {market}: **{p['selection']} {odds_str}**  ·  {conf}% conf  ·  +{ev}% EV  ·  {units}u\n"
                f"> 📚 {books_line}\n"
                + (f"> *{insight}*" if insight else "")
            )

        total_units = round(sum(p["units"] for p in picks), 1)
        avg_conf    = round(sum(p["confidence"] for p in picks) / len(picks) * 100)
        avg_ev      = round(sum(p["ev_pct"] for p in picks) / len(picks) * 100, 1)

        divider = "─" * 36
        embed = {
            "title":       f"🪨  HardRock Entry  ·  {date_str}",
            "description": f"\n{divider}\n\n" + f"\n\n{divider}\n\n".join(leg_lines) + f"\n\n{divider}",
            "color":       0xB71C1C,
            "fields": [
                {"name": "Picks",       "value": f"**{len(picks)}**",      "inline": True},
                {"name": "Total Units", "value": f"**{total_units}u**",    "inline": True},
                {"name": "Avg EV",      "value": f"**+{avg_ev}%**",        "inline": True},
                {"name": "Avg Conf",    "value": f"**{avg_conf}%**",       "inline": True},
                {"name": "Generated",   "value": now_str,                  "inline": True},
                {"name": "Action",      "value": "📲 Place on HardRock Bet", "inline": True},
            ],
            "footer": {"text": "Odds API · Sofascore · AI research · Bet responsibly"},
        }

        _run_async(_post({"embeds": [embed]}))
        logger.info("HardRock entry posted: %d picks, %.1f total units", len(picks), total_units)
        return {"picks": len(picks), "total_units": total_units}

    except Exception as exc:
        logger.error("HardRock entry generation failed: %s", exc)
        return {"error": str(exc)}


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
