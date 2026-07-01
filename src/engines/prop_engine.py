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
from src.core.timezone import et_naive

logger = logging.getLogger(__name__)

# Minimum thresholds for a prop to be recommended
MIN_PROP_CONFIDENCE = 0.60   # 60% — enough picks to fill entry cards
MIN_PROP_EV        = 0.03    # 3% edge minimum


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
    generated_at: str         = field(default_factory=lambda: et_naive().isoformat())


def analyse_prop(prop: dict, player_context: dict | None = None, loss_history: list[dict] | None = None) -> dict | None:
    """
    Ask AI to score a single prop Over/Under.
    Includes player context and — on retry after a loss — deeper data.
    """
    import json
    from src.engines.ai_engine import _call_json   # noqa: F401

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


def _sample_props(props: list[dict], max_per_sport: int = 30) -> list[dict]:
    """
    Sample a manageable subset of props per sport.
    Guarantees team props are included alongside player props.
    Prioritises game-day props (game_time set).
    """
    from collections import defaultdict
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for p in props:
        by_sport[p.get("sport_key", "unknown")].append(p)

    sampled = []
    for sport_props in by_sport.values():
        team_props   = [p for p in sport_props if p.get("is_team_prop")]
        player_props = [p for p in sport_props if not p.get("is_team_prop")]

        # Guarantee up to 8 team props per sport, rest filled with player props
        max_team   = min(len(team_props), 8)
        max_player = max_per_sport - max_team

        # Within each group, prefer props with a game_time
        def _prioritise(lst):
            return [p for p in lst if p.get("game_time")] + \
                   [p for p in lst if not p.get("game_time")]

        sampled.extend(_prioritise(team_props)[:max_team])
        sampled.extend(_prioritise(player_props)[:max_player])

    return sampled


def score_props(props: list[dict]) -> tuple[list[PropPick], list[PropPick]]:
    """
    Analyse props using a SINGLE batch OpenAI call per sport group.
    Reduces API calls from N (one per prop) to ~5 (one per sport).
    Samples top props per sport, sends them all in one prompt, gets back ranked picks.
    """
    import json
    from src.engines.ai_engine import _call_json
    from collections import defaultdict

    # Sample props — prioritise game-day props, cap per sport
    sampled = _sample_props(props, max_per_sport=25)
    logger.info("Prop engine: batch scoring %d props across sports", len(sampled))

    # Group by sport for focused analysis
    by_sport: dict[str, list[dict]] = defaultdict(list)
    for p in sampled:
        by_sport[p.get("sport_key", "unknown")].append(p)

    system = """You are an elite sports prop analyst. Given a list of player AND team prop lines,
identify the BEST bets (OVER or UNDER). Analyse BOTH player props and team props equally.

For PLAYER props consider: season averages vs line, recent form, matchup, injuries, usage, pace.
For TEAM props consider: team scoring average, opponent defence, pace, home/away splits, recent form.

Return ONLY valid JSON array of picks (empty array [] if none qualify):
[
  {
    "index": <int — index of prop in the input list>,
    "direction": "over"|"under",
    "confidence": <float 0.0-1.0>,
    "ev_pct": <float e.g. 0.05 = 5% edge>,
    "reasoning": "<2-3 sentences>",
    "key_factors": ["<factor1>", "<factor2>"],
    "parlay_friendly": true|false
  }
]

Only include props where confidence >= 0.55 and ev_pct >= 0.02.
Do NOT skip team props — they often have strong edge due to less public attention."""

    picks: list[PropPick] = []
    watchlist: list[PropPick] = []  # near-miss picks: 55–64% conf

    for sport_key, sport_props in by_sport.items():
        if not sport_props:
            continue

        # Build compact prop list for the prompt
        prop_list = [
            {
                "index":      i,
                "subject":    p.get("subject"),
                "stat":       p.get("stat"),
                "line":       p.get("line"),
                "team":       p.get("team", ""),
                "opponent":   p.get("opponent", ""),
                "game_time":  p.get("game_time", ""),
                "is_team":    p.get("is_team_prop", False),
            }
            for i, p in enumerate(sport_props)
        ]

        prompt = (
            f"Sport: {sport_key}\n\n"
            f"Analyse these {len(prop_list)} props and return your best picks:\n\n"
            f"```json\n{json.dumps(prop_list, indent=2)}\n```"
        )

        try:
            result = _call_json(prompt, system)
            if not result:
                continue

            # Result may be a list directly or wrapped
            if isinstance(result, dict):
                result = result.get("picks", result.get("results", []))
            if not isinstance(result, list):
                continue

            for item in result:
                idx = item.get("index")
                if idx is None or idx >= len(sport_props):
                    continue
                prop       = sport_props[idx]
                direction  = item.get("direction", "pass").lower()
                confidence = float(item.get("confidence", 0))
                ev_pct     = float(item.get("ev_pct", 0))

                if direction == "pass":
                    continue

                pick = PropPick(
                    source       = prop.get("source", "prizepicks"),
                    sport_key    = sport_key,
                    subject      = prop.get("subject", ""),
                    stat         = prop.get("stat", ""),
                    line         = float(prop.get("line", 0)),
                    direction    = direction,
                    confidence   = round(confidence, 4),
                    ev_pct       = round(ev_pct, 4),
                    reasoning    = item.get("reasoning", ""),
                    key_factors  = item.get("key_factors", []),
                    is_team_prop = prop.get("is_team_prop", False),
                    game_time    = prop.get("game_time", ""),
                    opponent     = prop.get("opponent", ""),
                    team         = prop.get("team", ""),
                )

                if confidence >= MIN_PROP_CONFIDENCE and ev_pct >= MIN_PROP_EV:
                    picks.append(pick)
                elif confidence >= 0.55 and ev_pct >= 0.02:
                    # Near-miss — goes to watchlist, not picks
                    watchlist.append(pick)

        except Exception as e:
            logger.warning("Batch prop scoring failed [%s]: %s", sport_key, e)

    picks.sort(key=lambda p: (p.ev_pct * p.confidence), reverse=True)
    watchlist.sort(key=lambda p: p.confidence, reverse=True)
    logger.info("Prop engine: %d props analysed → %d picks, %d on watchlist", len(props), len(picks), len(watchlist))

    return picks, watchlist


def record_prop_result(
    subject:      str,
    stat:         str,
    sport_key:    str,
    direction:    str,
    line:         float,
    actual_value: float,
    game_time:    str  = "",
    pick_dict:    dict | None = None,  # original PropPick dict for the alert
) -> str:
    """
    Grade a prop pick (over/under) against actual result.
    Saves to PropResult table and fires Discord alert for ALL outcomes.
    Returns 'won'|'lost'|'push'.
    """
    if actual_value > line:
        outcome = "won" if direction == "over" else "lost"
    elif actual_value < line:
        outcome = "won" if direction == "under" else "lost"
    else:
        outcome = "lost"  # exact line = lost (no push — CASHED/DEAD only)

    # ── Save to DB ────────────────────────────────────────────────────────────
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
                settled_at   = et_naive(),
            ))
    except Exception as e:
        logger.warning("Failed to save prop result: %s", e)

    # ── Fire Discord alert for every result (win, loss, push) ─────────────────
    try:
        alert_pick = pick_dict or {
            "subject":   subject,
            "stat":      stat,
            "sport_key": sport_key,
            "direction": direction,
            "line":      line,
            "game_time": game_time,
        }
        from src.workers.alert_worker import _run_async
        from src.discord_bot.bot import post_prop_result
        _run_async(post_prop_result(alert_pick, outcome, actual_value))
    except Exception as e:
        logger.warning("Failed to queue prop result alert: %s", e)

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
