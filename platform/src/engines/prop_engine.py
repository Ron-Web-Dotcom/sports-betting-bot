"""
Prop Engine — PrizePicks-specific pick analysis and learning loop.

Full cycle:
  1. Pull live PrizePicks props (all sports)
  2. Enrich each prop with player context (recent form, injuries, stats)
  3. AI scores each prop Over/Under with reasoning
  4. Gate filters low-confidence / low-EV props
  5. BET props posted to Discord with PP line + direction
  6. After game: settlement grades Over/Under result
  7. Losses feed back into the learning DB — AI gets deeper context next time
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Minimum thresholds for a prop to be recommended
MIN_PROP_CONFIDENCE = 0.60   # 60%
MIN_PROP_EV        = 0.03    # 3% edge


@dataclass
class PropPick:
    source:       str         # "prizepicks"
    sport_key:    str
    subject:      str         # player or team name
    stat:         str         # "Points", "Rushing Yards", etc.
    line:         float       # PP line (e.g. 24.5)
    direction:    str         # "over" | "under"
    confidence:   float       # 0.0–1.0
    ev_pct:       float       # estimated edge
    reasoning:    str
    key_factors:  list[str]   = field(default_factory=list)
    is_team_prop: bool        = False
    game_time:    str         = ""
    opponent:     str         = ""
    team:         str         = ""
    generated_at: str         = field(default_factory=lambda: datetime.utcnow().isoformat())


def analyse_prop(prop: dict, player_context: dict | None = None, loss_history: list[dict] | None = None) -> dict | None:
    """
    Ask AI to score a single prop Over/Under.
    Includes player context and — on retry after a loss — deeper data.
    """
    import json
    from src.engines.ai_engine import _call_json   # noqa: internal

    system = """You are an elite sports prop analyst. Given a player or team prop line,
decide whether to bet OVER or UNDER (or PASS).

Return ONLY valid JSON:
{
  "direction": "over"|"under"|"pass",
  "confidence": <float 0.0-1.0>,
  "ev_pct": <float — estimated edge as decimal, e.g. 0.05 = 5%>,
  "reasoning": "<3-4 sentences>",
  "key_factors": ["<factor1>", "<factor2>", "<factor3>"],
  "risk_flags": ["<concern or empty list>"],
  "parlay_friendly": true|false
}

Be conservative. Only recommend OVER/UNDER when there is genuine statistical edge.
Consider: recent form, season average vs line, injury status, matchup, pace, opponent defence."""

    payload: dict = {
        "prop": {
            "subject":      prop.get("subject"),
            "stat":         prop.get("stat"),
            "line":         prop.get("line"),
            "sport":        prop.get("sport_key"),
            "opponent":     prop.get("opponent"),
            "team":         prop.get("team"),
            "is_team_prop": prop.get("is_team_prop", False),
            "game_time":    prop.get("game_time"),
        }
    }

    if player_context:
        payload["recent_form"]  = player_context.get("recent_form", {})
        payload["season_stats"] = player_context.get("season_stats", {})
        payload["injuries"]     = player_context.get("injury_status", [])
        payload["vs_opponent"]  = player_context.get("vs_opponent", {})

    if loss_history:
        # Feed past failures so the AI learns what went wrong
        payload["past_losses"] = loss_history[:5]
        payload["learning_note"] = (
            "These are previous losses on this same player/stat. "
            "Identify what was missed and apply those lessons now."
        )

    prompt = f"Analyse this prop bet:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call_json(prompt, system)


def score_props(props: list[dict]) -> list[PropPick]:
    """
    Analyse a batch of props and return PropPick objects that pass the gate.
    Enriches each prop with player context + past loss history before AI call.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.apis.data_hub import build_player_context
    from src.db.session import get_db
    from src.db.models import PropResult

    picks: list[PropPick] = []

    def _process(prop: dict) -> PropPick | None:
        subject   = prop.get("subject", "")
        stat      = prop.get("stat", "")
        sport_key = prop.get("sport_key", "")

        # Enrich with player context (recent form, injuries, splits)
        try:
            ctx = build_player_context(
                player_name = subject,
                sport_key   = sport_key,
                opponent    = prop.get("opponent", ""),
            )
        except Exception:
            ctx = {}

        # Pull past losses for this subject+stat to feed the learning loop
        loss_history: list[dict] = []
        try:
            with get_db() as db:
                rows = db.query(PropResult).filter_by(
                    subject=subject, stat=stat, result="lost"
                ).order_by(PropResult.settled_at.desc()).limit(5).all()
                loss_history = [
                    {
                        "line":        r.line,
                        "direction":   r.direction,
                        "actual":      r.actual_value,
                        "game_time":   str(r.game_time),
                        "missed_by":   round(abs((r.actual_value or 0) - (r.line or 0)), 2),
                    }
                    for r in rows
                ]
        except Exception:
            pass  # PropResult table may not exist yet on first run

        ai = analyse_prop(prop, player_context=ctx, loss_history=loss_history or None)
        if not ai:
            return None

        direction  = ai.get("direction", "pass").lower()
        confidence = float(ai.get("confidence", 0))
        ev_pct     = float(ai.get("ev_pct", 0))

        if direction == "pass" or confidence < MIN_PROP_CONFIDENCE or ev_pct < MIN_PROP_EV:
            return None

        return PropPick(
            source       = prop.get("source", "prizepicks"),
            sport_key    = sport_key,
            subject      = subject,
            stat         = stat,
            line         = float(prop.get("line", 0)),
            direction    = direction,
            confidence   = round(confidence, 4),
            ev_pct       = round(ev_pct, 4),
            reasoning    = ai.get("reasoning", ""),
            key_factors  = ai.get("key_factors", []),
            is_team_prop = prop.get("is_team_prop", False),
            game_time    = prop.get("game_time", ""),
            opponent     = prop.get("opponent", ""),
            team         = prop.get("team", ""),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_process, p): p for p in props}
        for future in as_completed(futures, timeout=120):
            try:
                result = future.result()
                if result:
                    picks.append(result)
            except Exception as e:
                logger.warning("Prop analysis failed: %s", e)

    picks.sort(key=lambda p: (p.ev_pct * p.confidence), reverse=True)
    logger.info("Prop engine: %d props analysed → %d picks", len(props), len(picks))
    return picks


def record_prop_result(subject: str, stat: str, sport_key: str,
                       direction: str, line: float,
                       actual_value: float, game_time: str = "") -> str:
    """
    Grade a prop pick (over/under) against actual result.
    Saves to PropResult table for learning loop. Returns 'won'|'lost'|'push'.
    """
    if actual_value > line:
        outcome = "won" if direction == "over" else "lost"
    elif actual_value < line:
        outcome = "won" if direction == "under" else "lost"
    else:
        outcome = "push"

    try:
        from src.db.session import get_db
        from src.db.models import PropResult
        with get_db() as db:
            db.add(PropResult(
                subject      = subject,
                stat         = stat,
                sport_key    = sport_key,
                direction    = direction,
                line         = line,
                actual_value = actual_value,
                result       = outcome,
                game_time    = game_time,
                settled_at   = datetime.utcnow(),
            ))
    except Exception as e:
        logger.warning("Failed to save prop result: %s", e)

    return outcome


def get_prop_performance(subject: str | None = None, stat: str | None = None,
                         sport_key: str | None = None, limit: int = 50) -> dict:
    """
    Return hit rate and ROI for prop picks — overall or filtered by subject/stat/sport.
    Used in daily summary and self-improvement cycle.
    """
    try:
        from src.db.session import get_db
        from src.db.models import PropResult
        with get_db() as db:
            q = db.query(PropResult)
            if subject:   q = q.filter(PropResult.subject == subject)
            if stat:       q = q.filter(PropResult.stat == stat)
            if sport_key:  q = q.filter(PropResult.sport_key == sport_key)
            rows = q.order_by(PropResult.settled_at.desc()).limit(limit).all()

        if not rows:
            return {}

        total  = len(rows)
        wins   = sum(1 for r in rows if r.result == "won")
        losses = sum(1 for r in rows if r.result == "lost")
        return {
            "total":    total,
            "wins":     wins,
            "losses":   losses,
            "hit_rate": round(wins / total, 4),
            "roi":      round((wins - losses) / total, 4),
        }
    except Exception as e:
        logger.warning("get_prop_performance failed: %s", e)
        return {}
