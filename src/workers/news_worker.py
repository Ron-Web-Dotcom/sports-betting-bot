"""News/injury worker — fetches ESPN + Sleeper data, detects status changes, alerts on prop impact."""
import logging
from src.engines.news_engine import fetch_all_injuries, save_injuries
from src.apis.sleeper import get_all_injured_players

logger = logging.getLogger(__name__)

# Statuses that directly kill or hurt a prop
_CRITICAL   = {"out", "doubtful", "ir", "suspended"}
_WATCHLIST  = {"questionable", "gtd", "day-to-day", "limited"}

# How each status affects a same-player OVER prop
_PROP_IMPACT = {
    "out":         ("❌ AVOID",  "Player is OUT — do not bet OVER. Line will move down."),
    "doubtful":    ("⚠️ AVOID",  "Doubtful (75% chance of sitting). Treat as OUT."),
    "ir":          ("❌ AVOID",  "On Injured Reserve — definitely out."),
    "suspended":   ("❌ AVOID",  "Suspended — will not play."),
    "questionable":("👀 WATCH",  "Questionable — wait for the official injury report closer to tip."),
    "gtd":         ("👀 WATCH",  "Game-time decision — do NOT lock in until confirmed active."),
    "day-to-day":  ("👀 WATCH",  "Day-to-day — monitor. Line may move if he sits."),
    "limited":     ("⚠️ CAUTION","Limited in practice — may play reduced minutes. UNDER lean."),
}

# Teammate impact: if a star sits, their teammates get more usage
_USAGE_BOOST_SPORTS = {"basketball_nba", "basketball_ncaab", "americanfootball_nfl"}


def _is_sleep_time() -> bool:
    from datetime import datetime
    import zoneinfo
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return 3 <= et.hour < 5


def _load_prev_injuries(r, sport_key: str) -> dict[str, str]:
    """Load previous injury snapshot from Redis. Returns {player_name: status}."""
    import json
    raw = r.get(f"injuries:{sport_key}")
    if not raw:
        return {}
    return json.loads(raw)


def _save_current_injuries(r, sport_key: str, injuries: list[dict]) -> None:
    """Cache current injury snapshot in Redis (TTL 2h)."""
    import json
    snapshot = {i["player_name"]: i["status"].lower() for i in injuries if i.get("player_name")}
    r.setex(f"injuries:{sport_key}", 7200, json.dumps(snapshot))


def _detect_changes(prev: dict[str, str], curr: list[dict]) -> list[dict]:
    """
    Compare previous and current injury snapshots.
    Returns list of change dicts for players whose status changed.
    """
    changes = []
    curr_map = {i["player_name"]: i for i in curr if i.get("player_name")}

    for name, inj in curr_map.items():
        new_status = (inj.get("status") or "").lower()
        old_status = prev.get(name, "").lower()

        if new_status == old_status:
            continue  # no change

        # Only alert on meaningful status changes
        if new_status in _CRITICAL or new_status in _WATCHLIST or old_status in _CRITICAL:
            changes.append({
                "player":     name,
                "team":       inj.get("team", ""),
                "sport_key":  inj.get("sport", ""),
                "old_status": old_status or "active",
                "new_status": new_status,
                "detail":     inj.get("detail", ""),
                "is_critical": new_status in _CRITICAL,
            })

    # Players who were injured and are now cleared
    for name, old_status in prev.items():
        if old_status in _CRITICAL and name not in curr_map:
            changes.append({
                "player":     name,
                "team":       "",
                "sport_key":  "",
                "old_status": old_status,
                "new_status": "active",
                "detail":     "No longer listed on injury report.",
                "is_critical": False,
            })

    return changes


def _find_affected_props(player_name: str, team: str, sport_key: str, r) -> list[dict]:
    """
    Check Redis props cache for any active prop picks involving this player
    OR teammates (usage boost) on the same team.
    """
    import json
    affected = []
    raw = r.get("props:odds_api")
    if not raw:
        return []

    props = json.loads(raw)
    name_lower = player_name.lower()
    team_lower = team.lower()

    for prop in props:
        subject_lower = (prop.get("subject") or "").lower()
        prop_team_lower = (prop.get("team") or "").lower()

        # Direct match — this player has a prop
        if name_lower in subject_lower or subject_lower in name_lower:
            affected.append({**prop, "_match": "direct"})

        # Teammate match — same team, different player (usage boost candidate)
        elif (team_lower and team_lower in prop_team_lower
              and sport_key in _USAGE_BOOST_SPORTS):
            affected.append({**prop, "_match": "teammate"})

    return affected


def _build_injury_alert(change: dict, affected_props: list[dict]) -> dict | None:
    """Build a Discord embed dict for an injury/lineup change."""
    status      = change["new_status"]
    old_status  = change["old_status"]
    player      = change["player"]
    team        = change["team"]
    sport       = change["sport_key"].split("_")[-1].upper() if change["sport_key"] else ""
    detail      = change["detail"]

    action, reason = _PROP_IMPACT.get(status, ("ℹ️ UPDATE", "Status changed."))

    # Cleared from injury report
    if status == "active":
        action = "✅ CLEARED"
        reason = "Back on the injury report as active. Props are back on."

    color_map = {
        "❌ AVOID": 0xC62828, "⚠️ AVOID": 0xE53935,
        "👀 WATCH": 0xF57F17, "⚠️ CAUTION": 0xFDD835,
        "✅ CLEARED": 0x2E7D32, "ℹ️ UPDATE": 0x757575,
    }
    color = color_map.get(action, 0x757575)

    # Prop impact lines
    prop_lines = []
    direct  = [p for p in affected_props if p.get("_match") == "direct"]
    teammates = [p for p in affected_props if p.get("_match") == "teammate"]

    for p in direct[:3]:
        line = p.get("line")
        stat = p.get("stat", "")
        prop_lines.append(f"🎯 **YOUR PROP**: {player} {stat} {line} → {action}")

    if status in _CRITICAL and teammates:
        for p in teammates[:3]:
            subj = p.get("subject", "")
            stat = p.get("stat", "")
            line = p.get("line")
            prop_lines.append(
                f"📈 **USAGE BOOST**: {subj} {stat} {line} → lean OVER "
                f"(more minutes/touches with {player} out)"
            )

    if not prop_lines and not direct:
        return None  # no active props affected — skip alert

    fields = [
        {"name": "Status Change", "value": f"`{old_status.upper()}` → `{status.upper()}`"},
        {"name": "Team",          "value": f"{team} ({sport})"},
        {"name": "Action",        "value": action},
        {"name": "Why",           "value": reason},
    ]
    if detail:
        fields.append({"name": "Report Detail", "value": detail[:300], "inline": False})
    if prop_lines:
        fields.append({"name": "Prop Impact", "value": "\n".join(prop_lines), "inline": False})

    return {
        "title": f"🚨 Lineup Alert: {player} — {status.upper()}",
        "description": f"Status update affecting your active props.",
        "color": color,
        "fields": fields,
    }


def _merge_injury_sources(espn_injuries: list[dict], sleeper_injuries: list[dict]) -> list[dict]:
    """
    Merge ESPN + Sleeper injury reports into one deduplicated list.
    ESPN is primary (richer detail); Sleeper fills gaps ESPN misses.
    Deduplication is by player_name — ESPN entry wins on collision.
    """
    merged: dict[str, dict] = {}

    # Normalize Sleeper entries to the same shape as ESPN entries
    for s in sleeper_injuries:
        name = s.get("player_name", "")
        if not name:
            continue
        inj = (s.get("injury_status") or s.get("status") or "").lower()
        merged[name.lower()] = {
            "player_name": name,
            "team":        s.get("team", ""),
            "sport":       s.get("sport", ""),
            "status":      inj,
            "detail":      s.get("injury_notes", ""),
            "source":      "sleeper",
        }

    # ESPN entries overwrite Sleeper on name collision (ESPN has more detail)
    for e in espn_injuries:
        name = e.get("player_name", "")
        if name:
            merged[name.lower()] = {**e, "source": "espn"}

    return list(merged.values())


def fetch_and_save_news():
    if _is_sleep_time():
        return {"skipped": "sleep_mode"}
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

        # ── Fetch injury data from both ESPN and Sleeper in parallel ──────────
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        espn_result: dict  = {}
        sleeper_result: list[dict] = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            espn_fut    = pool.submit(fetch_all_injuries)
            sleeper_fut = pool.submit(get_all_injured_players)
            for fut in _as_completed([espn_fut, sleeper_fut], timeout=30):
                if fut is espn_fut:
                    try:
                        espn_result = fut.result()
                    except Exception as e:
                        logger.warning("ESPN injury fetch failed: %s", e)
                else:
                    try:
                        sleeper_result = fut.result()
                    except Exception as e:
                        logger.warning("Sleeper injury fetch failed: %s", e)

        espn_flat = [inj for lst in espn_result.values() for inj in lst]

        # Group Sleeper injuries by sport for per-sport change detection
        sleeper_by_sport: dict[str, list[dict]] = {}
        for inj in sleeper_result:
            sk = inj.get("sport", "")
            sleeper_by_sport.setdefault(sk, []).append(inj)

        # Merge and save to DB
        all_injuries = _merge_injury_sources(espn_flat, sleeper_result)
        save_injuries(all_injuries)

        # ── Detect changes and alert on prop-impacting updates ────────────────
        # Build combined per-sport view for change detection
        combined_by_sport: dict[str, list[dict]] = {}
        for sport_key, injuries in espn_result.items():
            combined_by_sport.setdefault(sport_key, []).extend(injuries)
        for sport_key, injuries in sleeper_by_sport.items():
            if sport_key and sport_key not in combined_by_sport:
                combined_by_sport[sport_key] = injuries  # Sleeper-only sport

        all_alerts = []
        for sport_key, injuries in combined_by_sport.items():
            prev = _load_prev_injuries(r, sport_key)
            changes = _detect_changes(prev, injuries)
            _save_current_injuries(r, sport_key, injuries)

            for change in changes:
                affected = _find_affected_props(
                    change["player"], change["team"], sport_key, r
                )
                alert = _build_injury_alert(change, affected)
                if alert:
                    all_alerts.append(alert)

        if all_alerts:
            from src.workers.alert_worker import send_lineup_alerts
            send_lineup_alerts(all_alerts)
            logger.info("Lineup alerts fired: %d changes affect active props", len(all_alerts))

        logger.info("News worker: %d injuries (ESPN:%d + Sleeper:%d), %d prop-impacting changes",
                    len(all_injuries), len(espn_flat), len(sleeper_result), len(all_alerts))
        return {
            "injuries":       len(all_injuries),
            "espn_injuries":  len(espn_flat),
            "sleeper_injuries": len(sleeper_result),
            "alerts":         len(all_alerts),
        }

    except Exception as exc:
        logger.error("News fetch failed: %s", exc)
        raise
