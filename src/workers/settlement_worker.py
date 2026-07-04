"""Settlement worker — settles picks and records CLV."""
import logging
import re
from datetime import datetime, timedelta
from src.db.session import get_db
from src.db.models import Pick, Game, Sport, BetResult
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

# Team name suffixes to strip when normalising for comparison
_SUFFIXES = re.compile(
    r'\b(fc|city|united|sc|cf|afc|bfc|sporting|athletics)\b', re.IGNORECASE
)


def _normalize_team_name(name: str) -> str:
    """Strip common suffixes and lowercase for fuzzy matching."""
    if not name:
        return ""
    name = _SUFFIXES.sub("", name).strip()
    return re.sub(r'\s+', ' ', name).lower().strip()


def _settlement_window() -> bool:
    """Skip only during sleep window (3–5 AM ET). Settle any time outside that."""
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return not (3 <= et.hour < 5)


def settle_completed_picks():
    if not _settlement_window():
        logger.debug("settle_completed_picks: outside window (3–5 AM ET), skipping")
        return {"skipped": "outside_settlement_window"}
    try:
        from src.engines.odds_engine import fetch_scores

        alerts_to_send = []

        with get_db() as db:
            from sqlalchemy import or_
            open_picks = db.query(Pick).filter(
                or_(Pick.result == BetResult.PENDING, Pick.result.is_(None)),
                Pick.recommendation == "BET",
                Pick.generated_at >= et_naive() - timedelta(days=14),
            ).all()

            if not open_picks:
                return {"settled": 0}

            # Fetch scores per sport, then build {game_external_id: score_dict} lookup
            game_ids = {p.game_id for p in open_picks if p.game_id}
            games = db.query(Game).filter(Game.id.in_(game_ids)).all()

            # Fetch Sport keys for those games
            sport_ids = {g.sport_id for g in games if g.sport_id}
            sport_key_map = {
                s.id: s.key
                for s in db.query(Sport).filter(Sport.id.in_(sport_ids)).all()
            }

            # Group external IDs by sport_key for batched API calls
            sport_to_external: dict[str, list[str]] = {}
            ext_to_db_id: dict[str, int] = {}
            for g in games:
                if g.external_id:
                    sk = sport_key_map.get(g.sport_id, "")
                    if sk:
                        sport_to_external.setdefault(sk, []).append(g.external_id)
                    ext_to_db_id[g.external_id] = g.id

            scores: dict[int, dict] = {}  # db game_id → score dict
            for sport_key, _ext_ids in sport_to_external.items():
                raw = fetch_scores(sport_key, days_from=14)
                for item in raw:
                    ext_id = item.get("id")
                    if ext_id and ext_id in ext_to_db_id:
                        scores[ext_to_db_id[ext_id]] = {
                            "completed": item.get("completed", False),
                            "status":    item.get("status", ""),
                            "push":      False,
                            "winner":    _extract_winner(item),
                            **_extract_score_details(item),
                        }

            settled_count = 0
            for pick in open_picks:
                score = scores.get(pick.game_id)
                if not score or not score.get("completed"):
                    continue

                winner = score.get("winner")
                result = _determine_result(pick, winner, score)
                if not result:
                    continue

                # Re-fetch within same session to avoid stale state
                db_pick = db.query(Pick).filter_by(id=pick.id).first()
                if not db_pick or db_pick.result not in (BetResult.PENDING, None, "pending"):
                    # Already settled — skip to prevent double-settlement
                    continue

                pnl = _calculate_pnl(db_pick, result)
                db_pick.result = result
                db_pick.actual_pnl_units = pnl
                db_pick.settled_at = et_naive()
                settled_count += 1

                # Collect data as plain dict BEFORE commit so the session
                # can close cleanly; we fire the alert outside the session.
                alerts_to_send.append((
                    {
                        "bet": db_pick.selection,
                        "sport": db_pick.sport,
                        "odds": db_pick.american_odds_at_gen,
                        "actual_pnl_units": pnl,
                    },
                    result,
                ))

        # Fire alerts AFTER the DB session has committed
        from src.workers.alert_worker import send_result_alert
        for pick_data, result in alerts_to_send:
            try:
                send_result_alert(pick_data, result)
            except Exception as e:
                logger.error("Alert dispatch failed for result %s: %s", result, e)

        logger.info("Settled %d picks", settled_count)
        return {"settled": settled_count}

    except Exception as exc:
        logger.error("Settlement failed: %s", exc)
        raise


def _extract_winner(score_item: dict) -> str | None:
    """Extract winning team name from an Odds API scores response item."""
    scores = score_item.get("scores") or []
    if not scores:
        return None
    try:
        sorted_scores = sorted(scores, key=lambda s: float(s.get("score", 0) or 0), reverse=True)
        if len(sorted_scores) >= 2 and sorted_scores[0].get("score") != sorted_scores[1].get("score"):
            return sorted_scores[0].get("name")
    except (ValueError, TypeError, KeyError):
        pass
    return None


def _extract_score_details(score_item: dict) -> dict:
    """
    Extract home/away scores and compute total_scored from an Odds API scores item.
    Returns a dict with home_score, away_score, home_team, total_scored.
    """
    scores_list = score_item.get("scores") or []
    home_team   = score_item.get("home_team", "")
    home_norm   = _normalize_team_name(home_team)

    home_score: float | None = None
    away_score: float | None = None

    for s in scores_list:
        try:
            val = float(s.get("score") or 0)
        except (ValueError, TypeError):
            continue
        name_norm = _normalize_team_name(s.get("name", ""))
        if home_norm and name_norm and (home_norm in name_norm or name_norm in home_norm):
            home_score = val
        else:
            away_score = val

    total_scored = (home_score + away_score) if (home_score is not None and away_score is not None) else None
    return {
        "home_score":   home_score,
        "away_score":   away_score,
        "home_team":    home_team,
        "total_scored": total_scored,
    }


def _determine_result(pick: Pick, winner: str | None, score: dict) -> str | None:
    """Map game outcome to pick result."""
    status = score.get("status", "")
    if status in ("canceled", "postponed"):
        # Don't settle canceled/postponed games — keep PENDING, cleanup handles old picks
        return None

    selection = (pick.selection or "").strip()
    market    = (pick.market or "h2h").lower()

    # ── Totals (Over/Under) ───────────────────────────────────────────────────
    if market == "totals" or selection.lower().startswith(("over", "under")):
        # Extract total_line from pick selection: "Over 221.5" → 221.5
        line_match = re.search(r'(\d+(?:\.\d+)?)', selection)
        total_line   = float(line_match.group(1)) if line_match else None
        total_scored = score.get("total_scored")  # actual combined score from _extract_score_details
        if total_line is None or total_scored is None:
            return None
        is_over = selection.lower().startswith("over")
        if abs(total_scored - total_line) < 0.1:
            return BetResult.LOST  # exact total = lost (no push)
        return BetResult.WON if (is_over and total_scored > total_line) or \
                        (not is_over and total_scored < total_line) else BetResult.LOST

    # ── Spreads ───────────────────────────────────────────────────────────────
    import re as _re
    spread_match = _re.search(r'([+-]?\d+(?:\.\d+)?)\s*$', selection)
    if market == "spreads" or spread_match:
        home_score = score.get("home_score")
        away_score = score.get("away_score")
        home_team  = score.get("home_team", "")
        if home_score is None or away_score is None or not spread_match:
            if market == "spreads":
                return None  # cannot settle spreads without scores and spread value
            # Fall through to moneyline matching below for ambiguous selections
        else:
            spread = float(spread_match.group(1))
            team_part = _re.sub(r'[+-]?\d+(?:\.\d+)?\s*$', '', selection).strip()
            team_norm = _normalize_team_name(team_part)
            home_norm = _normalize_team_name(home_team)
            is_home   = home_norm and team_norm and (
                home_norm in team_norm or team_norm in home_norm
            )
            margin = (home_score - away_score) if is_home else (away_score - home_score)
            covered = margin + spread
            if abs(covered) < 0.1:
                return BetResult.LOST  # exact spread = lost (no push)
            return BetResult.WON if covered > 0 else BetResult.LOST

    # ── Moneyline (h2h) ───────────────────────────────────────────────────────
    if not winner:
        return None

    selection_norm = _normalize_team_name(selection)
    winner_norm    = _normalize_team_name(winner)

    if winner_norm and selection_norm and (
        winner_norm in selection_norm or selection_norm in winner_norm
    ):
        return BetResult.WON

    return BetResult.LOST


def _calculate_pnl(pick: Pick, result: str) -> float:
    units = pick.units or 1
    # result comes from _determine_result which returns BetResult enum values
    # BetResult(str, Enum) so string comparison works; accept both "won"/"cashed" and "lost"/"dead"
    result_lower = str(result).lower()
    if result_lower in ("won", "cashed"):
        from src.engines.ev_engine import american_to_decimal
        dec = american_to_decimal(pick.american_odds_at_gen or -110)
        return round((dec - 1) * units, 2)
    elif result_lower in ("lost", "dead"):
        return -units
    return 0.0  # unexpected result state — treated as break-even


def record_closing_lines():
    """Snapshot current odds for open picks — used later for CLV calculation.
    Only runs when games are active (8 AM–midnight ET)."""
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    if not (8 <= et.hour < 24):
        logger.debug("record_closing_lines: no active games outside 8 AM–midnight ET, skipping")
        return {"skipped": "outside_window"}
    from src.engines.clv_engine import record_clv
    from src.engines.odds_engine import get_latest_snapshots_by_game

    # Extract plain values before session closes — avoids DetachedInstanceError
    with get_db() as db:
        from sqlalchemy import or_
        rows = db.query(Pick.id, Pick.game_id, Pick.american_odds_at_gen).filter(
            or_(Pick.result == BetResult.PENDING, Pick.result.is_(None)),
            Pick.recommendation == "BET",
        ).all()

    if not rows:
        return {"recorded": 0}

    snapshots = get_latest_snapshots_by_game()
    recorded = 0

    for pick_id, game_id, odds_at_gen in rows:
        snap_list = snapshots.get(game_id, [])
        if not snap_list:
            continue
        closing_odds = snap_list[0].get("best_odds")
        if closing_odds:
            record_clv(pick_id, odds_at_gen, closing_odds)
            recorded += 1

    return {"recorded": recorded}
