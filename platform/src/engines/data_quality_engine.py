"""Data quality engine — assess event/odds data quality and adjust confidence."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DataQualityReport:
    score: float
    issues: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    data_age_seconds: float = 0.0
    source_conflicts: list[str] = field(default_factory=list)


def assess_event_quality(event: dict, odds_snapshots: list, injuries: list) -> DataQualityReport:
    issues: list[str] = []
    missing_fields: list[str] = []
    source_conflicts: list[str] = []
    score = 1.0

    # check required fields
    for field_name in ("commence_time", "home_team", "away_team", "markets"):
        if not event.get(field_name):
            missing_fields.append(field_name)
            issues.append(f"missing field: {field_name}")
            score -= 0.1

    # data age check
    data_age_seconds = 0.0
    if odds_snapshots:
        now = datetime.now(timezone.utc)
        latest_ts = None
        for snap in odds_snapshots:
            ts = snap.get("captured_at")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
        if latest_ts:
            data_age_seconds = (now - latest_ts).total_seconds()
            if data_age_seconds > 600:
                issues.append(f"data is {data_age_seconds:.0f}s old (>10 min)")
                score -= 0.15

    # check minimum books
    books = set()
    for snap in odds_snapshots:
        b = snap.get("book") or snap.get("bookmaker")
        if b:
            books.add(b)
    if len(books) < 3:
        issues.append(f"only {len(books)} books have odds (need >=3)")
        score -= 0.1

    # spread conflict check across books
    spreads: dict[str, float] = {}
    for snap in odds_snapshots:
        market = snap.get("market", "")
        if "spread" in market.lower():
            book = snap.get("book") or snap.get("bookmaker", "unknown")
            line = snap.get("line_value") or snap.get("spread")
            if line is not None:
                spreads[book] = float(line)
    if len(spreads) >= 2:
        vals = list(spreads.values())
        spread_range = max(vals) - min(vals)
        if spread_range > 3:
            conflict_msg = f"spread varies {spread_range:.1f}pts across books"
            source_conflicts.append(conflict_msg)
            issues.append(conflict_msg)
            score -= 0.15

    score = max(0.0, min(1.0, score))
    return DataQualityReport(
        score=score,
        issues=issues,
        missing_fields=missing_fields,
        data_age_seconds=data_age_seconds,
        source_conflicts=source_conflicts,
    )


def quality_to_confidence_adj(score: float) -> float:
    # linear mapping: score 0.5->0.5, score 1.0->1.0; below 0.5 clamp to 0.5
    if score <= 0.5:
        return 0.5
    return 0.5 + (score - 0.5)


def run_system_quality_check() -> dict:
    from src.db.session import get_db
    from src.db.models import Game, OddsSnapshot
    from datetime import timedelta

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=6)

    with get_db() as db:
        games = db.query(Game).filter(Game.commence_time >= now).limit(20).all()
        results = []
        for game in games:
            snapshots = db.query(OddsSnapshot).filter(
                OddsSnapshot.game_id == game.id,
                OddsSnapshot.captured_at >= cutoff,
            ).all()
            snap_dicts = [
                {
                    "book": s.book,
                    "market": s.market,
                    "line_value": s.line_value,
                    "captured_at": s.captured_at,
                }
                for s in snapshots
            ]
            event = {
                "commence_time": game.commence_time,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "markets": snap_dicts or None,
            }
            report = assess_event_quality(event, snap_dicts, [])
            results.append({
                "game_id": game.id,
                "game": f"{game.home_team} vs {game.away_team}",
                "quality_score": report.score,
                "issues": report.issues,
            })

    avg_score = sum(r["quality_score"] for r in results) / len(results) if results else 1.0
    return {
        "games_checked": len(results),
        "avg_quality_score": avg_score,
        "results": results,
    }
