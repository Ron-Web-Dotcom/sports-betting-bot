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


@app.task(bind=True, max_retries=2, default_retry_delay=60)
def generate_picks(self):
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
