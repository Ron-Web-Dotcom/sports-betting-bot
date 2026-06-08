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
from src.workers.celery_app import app
from src.db.session import get_db
from src.db.models import Game

logger = logging.getLogger(__name__)


def _post_parlay_bundles(pick_dicts: list[dict], pp_task, hr_task) -> None:
    """
    Bundle picks into PP (max 6) and HardRock (max 4) parlay cards.
    PP picks: prop picks (Over/Under)
    HardRock picks: game picks with american_odds
    Fires one card per bundle — only if 2+ picks available.
    """
    # PP entry: sort by confidence*ev desc, take top 6
    pp_picks = sorted(pick_dicts, key=lambda p: (p.get("confidence", 0) * p.get("ev_pct", 0)), reverse=True)
    if len(pp_picks) >= 2:
        # Post the best 2, 3, 4, 5, 6-leg combos as a single recommended entry
        n = min(len(pp_picks), 6)
        pp_task.delay(pp_picks[:n])

    # HardRock: picks that have american_odds (game picks, not props)
    hr_picks = [p for p in pick_dicts if p.get("american_odds") or p.get("odds")]
    hr_picks = sorted(hr_picks, key=lambda p: p.get("confidence", 0), reverse=True)
    if len(hr_picks) >= 2:
        n = min(len(hr_picks), 4)
        hr_task.delay(hr_picks[:n])


def _is_sleep_time() -> bool:
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


@app.task(bind=True, max_retries=2, default_retry_delay=60)
def generate_picks(self):
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
            send_pick_alerts.delay(bet_picks)

        logger.info("Pick generation complete: %d BET picks", len(bet_picks))
        return {"total_games": len(snapshots), "bet_picks": len(bet_picks)}

    except Exception as exc:
        logger.error("Pick generation failed: %s", exc)
        raise self.retry(exc=exc)


@app.task
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

    # ── 1. Force-refresh all props (lines just opened) ────────────────────────
    try:
        from src.apis.prizepicks import get_all_projections
        from src.apis.underdog import get_all_lines
        from src.apis.sleeper import get_all_projections as sleeper_projections
        from src.core.config import REDIS_URL
        import redis as _redis
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        _fetchers = {
            "prizepicks": get_all_projections,
            "underdog":   get_all_lines,
            "sleeper":    sleeper_projections,
        }
        _results: dict = {}
        with ThreadPoolExecutor(max_workers=3) as _pool:
            _futs = {_pool.submit(fn): name for name, fn in _fetchers.items()}
            for _fut in _as_completed(_futs, timeout=30):
                _name = _futs[_fut]
                try:
                    _results[_name] = _fut.result()
                except Exception as _e:
                    logger.warning("Morning fetch [%s] failed: %s", _name, _e)
                    _results[_name] = []

        pp_props = _results.get("prizepicks", [])
        ud_props = _results.get("underdog", [])
        sl_props = _results.get("sleeper", [])
        for _name, _items in _results.items():
            r.setex(f"props:{_name}", 900, json.dumps(_items or []))
        all_props = pp_props + ud_props + sl_props
    except Exception as e:
        logger.warning("Morning props fetch failed: %s", e)
        all_props = []

    # ── 2. AI picks on fresh props ────────────────────────────────────────────
    picks = []
    if all_props:
        try:
            from src.engines.prop_engine import score_props
            picks = score_props(all_props)
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
            {"name": "Sources Checked", "value": "PrizePicks · Underdog · HardRock (via Odds API)", "inline": False},
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

        # Individual pick alerts + parlay bundles
        from src.workers.alert_worker import (
            send_prop_pick_alerts, send_pp_parlay_alert, send_hardrock_parlay_alert
        )
        pick_dicts = [dataclasses.asdict(p) for p in picks]
        send_prop_pick_alerts.delay(pick_dicts)
        _post_parlay_bundles(pick_dicts, send_pp_parlay_alert, send_hardrock_parlay_alert)

    _run_async(_post({"embeds": embeds}))
    logger.info("Morning props brief sent: %d picks at 8 AM ET", len(picks))
    return {"picks": len(picks), "props_fetched": len(all_props)}


@app.task(bind=True, max_retries=2, default_retry_delay=60)
def scan_and_pick_props(self):
    """
    Full PrizePicks prop pick cycle — runs every 30 min (skips during sleep).

    1. Pull live props from Redis cache (populated by scan_player_props)
    2. AI analyses each prop: Over or Under?
    3. Gate filters low-confidence / low-EV props
    4. BET props posted to Discord
    5. Results tracked in PropResult table for learning loop
    """
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        import json, dataclasses

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

        # Filter out completed/in-progress games
        props = [p for p in props if p.get("status", "").lower() not in ("final", "completed", "in progress")]

        # Filter out props with no subject name
        props = [p for p in props if p.get("subject", "").strip()]

        # Filter props to only sports with games today
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        def _is_today_or_future(p: dict) -> bool:
            gt = p.get("game_time", "")
            if not gt:
                return True  # no time set — include (PrizePicks often omits it)
            try:
                from dateutil.parser import parse as _parse
                t = _parse(gt)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                return t >= now
            except Exception:
                return True
        props = [p for p in props if _is_today_or_future(p)]

        from src.engines.prop_engine import score_props
        picks = score_props(props)

        if picks:
            # Cap at top 5 picks per scan to avoid Discord spam
            picks = picks[:5]
            pick_dicts = [dataclasses.asdict(p) for p in picks]
            from src.workers.alert_worker import (
                send_prop_pick_alerts, send_pp_parlay_alert, send_hardrock_parlay_alert
            )
            # Individual pick alerts
            send_prop_pick_alerts.delay(pick_dicts)

            # PP parlay — best 2 to 6 picks bundled
            _post_parlay_bundles(pick_dicts, send_pp_parlay_alert, send_hardrock_parlay_alert)
            logger.info("Prop picks: %d picks, parlays posted", len(picks))

        return {"props_analysed": len(props), "picks": len(picks)}

    except Exception as exc:
        logger.error("scan_and_pick_props failed: %s", exc)
        raise self.retry(exc=exc)


@app.task
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
            send_parlay_alerts.delay([dataclasses.asdict(p) for p in parlays])

        return {"parlays": len(parlays)}

    except Exception as exc:
        logger.error("Parlay generation failed: %s", exc)
        return {"error": str(exc)}
