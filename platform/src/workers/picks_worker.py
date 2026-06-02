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
        from src.engines.odds_engine import get_latest_snapshots_by_game
        from src.engines.news_engine import get_recent_injuries
        from src.engines.ev_engine import compute_ev, assign_units, EVResult
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

            ai = analyse_pick(event, game_injuries, [], odds_by_book)
            if not ai:
                continue

            from src.engines.ev_engine import american_to_decimal, implied_prob, remove_vig
            best_odds = best_snap.get("best_odds", -110)
            dec = american_to_decimal(best_odds)
            fair_prob = remove_vig([best_odds])[0] if odds_by_book else implied_prob(dec)

            ev = compute_ev(fair_prob, dec, best_odds)
            units = assign_units(ev.ev_pct)
            ev_result = EVResult(
                fair_prob=fair_prob,
                implied_prob=ev.implied_prob,
                ev_pct=ev.ev_pct,
                decimal_odds=dec,
                american_odds=best_odds,
                is_positive_ev=ev.is_positive_ev,
            )

            confidence = compute_confidence(
                ai_score=ai.get("confidence", 0.5),
                statistical_score=ai.get("statistical_score", 0.5),
                market_score=ai.get("market_score", 0.5),
                line_score=0.5,
            )
            risk = assess(ev_result, confidence, game_injuries)

            comparison = compare_all_markets(snap_list)

            pick = build_recommendation(
                sport=event["sport_key"],
                game=f"{event['home_team']} vs {event['away_team']}",
                bet=ai.get("selection", ""),
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
                pick_dict = pick.__dict__.copy()
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

        with get_db() as db:
            today_picks = db.query(Pick).filter(
                Pick.generated_at >= datetime.utcnow() - timedelta(hours=12),
                Pick.recommendation == "BET",
            ).all()

        if len(today_picks) < 2:
            return {"parlays": 0}

        picks_dicts = [
            {
                "id": p.id, "bet": p.selection, "sport": p.sport,
                "odds": p.american_odds_at_gen, "ev_pct": p.ev_pct,
                "confidence_pct": p.confidence_pct,
            }
            for p in today_picks
        ]
        parlays = find_best_parlays(picks_dicts, max_legs=4, top_n=3)

        if parlays:
            from src.workers.alert_worker import send_parlay_alerts
            send_parlay_alerts.delay([p.__dict__ for p in parlays])

        return {"parlays": len(parlays)}

    except Exception as exc:
        logger.error("Parlay generation failed: %s", exc)
        return {"error": str(exc)}
