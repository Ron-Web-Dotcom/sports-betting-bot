"""
Discord integration — webhook-only.

All alerts are sent as HTTP POST to DISCORD_WEBHOOK_URL.
No bot token, no slash commands, no discord.py bot process needed.
"""
import asyncio
import json
import logging
import httpx
from src.core.config import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds
_MAX_RETRIES = 3


# ── Core send ──────────────────────────────────────────────────────────────────

async def _post(payload: dict) -> bool:
    """POST payload to the webhook URL. Respects Discord rate-limit headers."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — alert suppressed")
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for attempt in range(_MAX_RETRIES):
                resp = await client.post(
                    DISCORD_WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 204:
                    return True
                if resp.status_code == 429:
                    # Rate limited — honour retry_after before next attempt
                    try:
                        retry_after = float(resp.json().get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    logger.warning("Discord rate limit — waiting %.1fs (attempt %d)", retry_after, attempt + 1)
                    await asyncio.sleep(retry_after + 0.1)
                    continue
                logger.error("Webhook POST failed: %s %s", resp.status_code, resp.text[:200])
                return False
        logger.error("Webhook POST failed after %d retries (rate limited)", _MAX_RETRIES)
        return False
    except httpx.HTTPError as e:
        logger.error("Webhook HTTP error: %s", e)
        return False


def _embed(title: str, description: str, color: int, fields: list[dict] | None = None) -> dict:
    """Build a Discord embed dict."""
    embed: dict = {"title": title[:256], "description": description[:4096], "color": color}
    if fields:
        embed["fields"] = [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
            for f in fields[:25]
        ]
    return embed


# ── Public API ─────────────────────────────────────────────────────────────────

async def post_prop_pick(pick: dict) -> None:
    """Discord prop pick card — mirrors the PrizePicks card layout."""
    direction  = (pick.get("direction") or "over").upper()
    arrow      = "↑" if direction == "OVER" else "↓"
    color      = 0x1B5E20 if direction == "OVER" else 0xB71C1C
    conf_pct   = round((pick.get("confidence") or 0) * 100)
    ev_pct     = round((pick.get("ev_pct") or 0) * 100, 1)
    line       = pick.get("line", "—")
    stat       = pick.get("stat", "")
    subject    = pick.get("subject", "Unknown")
    team       = pick.get("team", "")
    opponent   = pick.get("opponent", "")
    game_time  = pick.get("game_time", "")
    is_team    = pick.get("is_team_prop", False)

    # Sport label — match PP style (WORLD CUP, NBA, NFL, etc.)
    sport_key  = pick.get("sport_key", "")
    _SPORT_LABELS = {
        "soccer_fifa_world_cup":          "WORLD CUP",
        "basketball_nba":                 "NBA",
        "americanfootball_nfl":           "NFL",
        "baseball_mlb":                   "MLB",
        "icehockey_nhl":                  "NHL",
        "soccer_epl":                     "PREMIER LEAGUE",
        "soccer_usa_mls":                 "MLS",
        "basketball_ncaab":               "NCAAB",
        "americanfootball_ncaaf":         "NCAAF",
        "mma_mixed_martial_arts":         "UFC/MMA",
        "tennis_atp_french_open":         "TENNIS",
        "golf_masters_tournament_winner": "GOLF",
    }
    sport_label = _SPORT_LABELS.get(sport_key, sport_key.split("_")[-1].upper())

    # Matchup line (e.g. "United States vs England")
    matchup = f"{team} vs {opponent}" if team and opponent else (team or opponent or "—")

    # Card-style title: arrow + line + stat (mirrors PP card)
    title = f"{arrow} {line} {stat}  ·  {subject}"

    # Subtitle row: sport • matchup • time
    time_str = ""
    if game_time:
        try:
            from datetime import datetime
            import zoneinfo
            dt = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
            dt_et = dt.astimezone(zoneinfo.ZoneInfo("America/New_York"))
            time_str = dt_et.strftime("%-I:%M %p ET")
        except Exception:
            time_str = game_time[:16]

    desc_parts = [f"**{sport_label}**"]
    if matchup and matchup != "—":
        desc_parts.append(matchup)
    if time_str:
        desc_parts.append(time_str)
    description = "  ·  ".join(desc_parts)

    # Fields — clean, compact
    fields = [
        {"name": "Direction",   "value": f"**{arrow} {direction}**",    "inline": True},
        {"name": "Line",        "value": f"**{line}** {stat}",          "inline": True},
        {"name": "Confidence",  "value": f"{conf_pct}%",                "inline": True},
        {"name": "Edge",        "value": f"+{ev_pct}%",                 "inline": True},
        {"name": "Type",        "value": "Team Prop" if is_team else "Player Prop", "inline": True},
        {"name": "Source",      "value": (pick.get("source") or "prizepicks").title(), "inline": True},
    ]

    factors = pick.get("key_factors") or []
    if factors:
        fields.append({
            "name": "Why",
            "value": "\n".join(f"• {f}" for f in factors[:3]),
            "inline": False,
        })

    reasoning = (pick.get("reasoning") or "")[:400]
    if reasoning:
        fields.append({"name": "Analysis", "value": reasoning, "inline": False})

    embed = _embed(title=title, description=description, color=color, fields=fields)
    await _post({"embeds": [embed]})


async def post_prop_result(pick: dict, result: str, actual: float) -> None:
    """Discord notification when a prop pick is settled — fires for wins, losses, and pushes."""
    icons   = {"won": "✅", "lost": "❌", "push": "➖"}
    colors  = {"won": 0x2E7D32, "lost": 0xC62828, "push": 0x757575}
    icon    = icons.get(result, "❓")
    color   = colors.get(result, 0x757575)
    line    = pick.get("line", 0)
    missed  = round(actual - line, 2)
    sport   = pick.get("sport_key", "—").split("_")[-1].upper()
    direction = pick.get("direction", "").upper()

    if result == "push":
        diff_str = "exact"
    elif result == "won":
        diff_str = f"{missed:+.1f} ({'over' if actual > line else 'under'} by {abs(missed)})"
    else:
        diff_str = f"{missed:+.1f} — missed by {abs(missed)}"

    fields = [
        {"name": "Result",    "value": f"{icon} {result.upper()}"},
        {"name": "Sport",     "value": sport},
        {"name": "Picked",    "value": f"{direction} {line}"},
        {"name": "Actual",    "value": str(actual)},
        {"name": "Difference","value": diff_str},
    ]

    if result == "lost":
        fields.append({
            "name":  "📚 Learning",
            "value": "This loss has been logged. The AI will use it as context next time it analyses this player/stat.",
            "inline": False,
        })

    embed = _embed(
        title=f"{icon} Prop Result: {pick.get('subject')} {pick.get('stat')}",
        description=f"**{direction} {line}** → Actual: **{actual}** ({diff_str})",
        color=color,
        fields=fields,
    )
    await _post({"embeds": [embed]})


# ── PP payout multipliers (Power Play — all legs must hit) ────────────────────
_PP_MULTIPLIERS = {2: 3, 3: 5, 4: 10, 5: 20, 6: 40}

_SPORT_LABELS_SHORT = {
    "soccer_fifa_world_cup":          "WORLD CUP",
    "basketball_nba":                 "NBA",
    "americanfootball_nfl":           "NFL",
    "baseball_mlb":                   "MLB",
    "icehockey_nhl":                  "NHL",
    "soccer_epl":                     "EPL",
    "soccer_usa_mls":                 "MLS",
    "basketball_ncaab":               "NCAAB",
    "americanfootball_ncaaf":         "NCAAF",
    "mma_mixed_martial_arts":         "UFC",
    "tennis_atp_french_open":         "TENNIS",
    "golf_masters_tournament_winner": "GOLF",
}


async def post_pp_parlay(picks: list[dict]) -> None:
    """PrizePicks multi-pick entry card — 2 to 6 legs, with per-leg reasoning."""
    if not picks:
        return
    n        = min(len(picks), 6)
    picks    = picks[:n]
    mult     = _PP_MULTIPLIERS.get(n, 40)
    avg_conf = round(sum((p.get("confidence") or 0) for p in picks) / n * 100)

    # Build one block per leg: header line + top reason + top key factor
    leg_blocks = []
    for i, p in enumerate(picks, 1):
        arrow   = "↑" if (p.get("direction") or "over").lower() == "over" else "↓"
        sport   = _SPORT_LABELS_SHORT.get(p.get("sport_key", ""), "")
        subject = p.get("subject", "?")
        stat    = p.get("stat", "")
        line    = p.get("line", "?")
        conf    = round((p.get("confidence") or 0) * 100)
        ev      = round((p.get("ev_pct") or 0) * 100, 1)

        # Pull top key factor (first one) as the quick reason
        factors   = p.get("key_factors") or []
        reasoning = (p.get("reasoning") or "").strip()
        # Use first key factor if available, else first sentence of reasoning
        if factors:
            top_reason = factors[0]
        elif reasoning:
            top_reason = reasoning.split(".")[0].strip()
        else:
            top_reason = "—"

        leg_blocks.append(
            f"`{i}.` {arrow} **{subject}** — {line} {stat}  ·  {sport}  ·  {conf}% conf  ·  +{ev}% edge\n"
            f"     └ {top_reason}"
        )

    fields = [
        {"name": "Legs",       "value": str(n),         "inline": True},
        {"name": "Multiplier", "value": f"**{mult}x**", "inline": True},
        {"name": "Avg Conf",   "value": f"{avg_conf}%", "inline": True},
        {
            "name":  "Entry Types",
            "value": "**Power Play** — all must hit → full multiplier\n**Flex Play** — 1 miss allowed → reduced payout",
            "inline": False,
        },
        {"name": "⚠️", "value": "Place manually on PrizePicks. Max 6 picks.", "inline": False},
    ]
    await _post({"embeds": [_embed(
        title=f"🏆 PrizePicks Entry — {n} Picks  ·  {mult}x Payout",
        description="\n".join(leg_blocks),
        color=0x1565C0,
        fields=fields,
    )]})


async def post_hardrock_parlay(picks: list[dict]) -> None:
    """HardRock parlay card — 2 to 4 legs, combined odds + per-leg reasoning."""
    if not picks:
        return
    n     = min(len(picks), 4)
    picks = picks[:n]

    combined_decimal = 1.0
    for p in picks:
        odds = p.get("american_odds") or p.get("odds") or -110
        try:
            dec = (odds / 100 + 1) if odds > 0 else (100 / abs(odds) + 1)
        except Exception:
            dec = 1.91
        combined_decimal *= dec

    if combined_decimal >= 2.0:
        combined_american = int((combined_decimal - 1) * 100)
    else:
        combined_american = int(-100 / (combined_decimal - 1))

    odds_str       = f"+{combined_american}" if combined_american > 0 else str(combined_american)
    example_payout = round(10 * combined_decimal, 2)

    leg_blocks = []
    for i, p in enumerate(picks, 1):
        sport    = _SPORT_LABELS_SHORT.get(p.get("sport_key", ""), p.get("sport", ""))
        bet      = p.get("bet") or p.get("selection") or p.get("subject", "?")
        leg_odds = p.get("american_odds") or p.get("odds", "?")
        lo_str   = f"+{leg_odds}" if isinstance(leg_odds, int) and leg_odds > 0 else str(leg_odds)
        conf     = round((p.get("confidence_pct") or p.get("confidence") or 0) * (1 if (p.get("confidence") or 0) <= 1 else 0.01))

        # Per-leg reason
        factors   = p.get("key_factors") or []
        reasoning = (p.get("reasoning") or p.get("ai_reasoning") or "").strip()
        if factors:
            top_reason = factors[0]
        elif reasoning:
            top_reason = reasoning.split(".")[0].strip()
        else:
            top_reason = "—"

        leg_blocks.append(
            f"`{i}.` **{bet}**  ·  {lo_str}  ·  {sport}  ·  {conf}% conf\n"
            f"     └ {top_reason}"
        )

    fields = [
        {"name": "Legs",          "value": str(n),               "inline": True},
        {"name": "Combined Odds", "value": odds_str,             "inline": True},
        {"name": "Payout on $10", "value": f"${example_payout}", "inline": True},
        {"name": "⚠️", "value": "Place manually on HardRock Bet. Max 4 legs.", "inline": False},
    ]
    await _post({"embeds": [_embed(
        title=f"🎰 HardRock Parlay — {n} Legs  ·  {odds_str}",
        description="\n".join(leg_blocks),
        color=0x6A1B9A,
        fields=fields,
    )]})


async def post_pick(pick: dict) -> None:
    rec = pick.get("recommendation", "PASS")
    color = 0x2E7D32 if rec == "BET" else 0x757575
    units = min(max(int(pick.get("units", 0)), 0), 5)
    unit_bar = "█" * units + "░" * (5 - units)

    fields = [
        {"name": "Game",       "value": pick.get("game", "—")},
        {"name": "Sport",      "value": pick.get("sport", "—")},
        {"name": "Odds",       "value": f"{pick.get('odds', 0):+d}"},
        {"name": "Units",      "value": f"{unit_bar} ({units}/5)"},
        {"name": "EV",         "value": f"{pick.get('ev_pct', 0):.1%}"},
        {"name": "Confidence", "value": f"{pick.get('confidence_pct', 0):.0f}%"},
        {"name": "Best Book",  "value": pick.get("best_book", "—")},
    ]
    key_factors = pick.get("key_factors", [])
    if key_factors:
        fields.append({"name": "Key Factors",
                        "value": "\n".join(f"• {f}" for f in key_factors[:3]),
                        "inline": False})
    risk_flags = pick.get("risk_flags", [])
    if risk_flags:
        fields.append({"name": "Risk Flags",
                        "value": "\n".join(f"• {f}" for f in risk_flags[:3]),
                        "inline": False})

    embed = _embed(
        title=f"[{rec}] {pick.get('bet', 'Unknown')}",
        description=pick.get("reasoning", ""),
        color=color,
        fields=fields,
    )
    await _post({"embeds": [embed]})


async def post_result(pick: dict, result: str) -> None:
    colors = {"won": 0x2E7D32, "lost": 0xC62828, "push": 0xF57F17}
    labels = {"won": "WIN", "lost": "LOSS", "push": "PUSH"}
    pnl = pick.get("actual_pnl_units", 0)
    fields = [
        {"name": "Game",  "value": pick.get("game", "—")},
        {"name": "Sport", "value": pick.get("sport", "—")},
        {"name": "Odds",  "value": f"{pick.get('odds', 0):+d}"},
    ]
    clv = pick.get("clv_pct")
    if clv is not None:
        fields.append({"name": "CLV", "value": f"{clv:+.2%}"})

    embed = _embed(
        title=f"[{labels.get(result, (result or 'UNKNOWN').upper())}] {pick.get('bet', 'Unknown')}",
        description=f"P&L: **{(pnl or 0):+.2f}u**",
        color=colors.get(result, 0x757575),
        fields=fields,
    )
    await _post({"embeds": [embed]})


async def post_prop_changes(changes: list[dict]) -> None:
    """Send a Discord alert for prop line changes (moved, added, removed)."""
    if not changes:
        return

    ICONS = {"moved": "📊", "added": "🆕", "removed": "❌"}
    COLORS = {"moved": 0xFDD835, "added": 0x43A047, "removed": 0xE53935}

    # Group by source so one embed per source (max 10 changes shown per source)
    from collections import defaultdict
    by_source: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        by_source[c.get("source", "props")].append(c)

    embeds = []
    for source, items in by_source.items():
        lines = []
        for c in items[:10]:
            icon    = ICONS.get(c["change_type"], "•")
            subject = c.get("subject", "Unknown")
            stat    = c.get("stat", "")
            sport   = c.get("sport_key", "").split("_")[-1].upper()
            if c["change_type"] == "moved":
                lines.append(
                    f"{icon} **{subject}** {stat} `{c['old_line']} → {c['new_line']}` ({sport})"
                )
            elif c["change_type"] == "added":
                lines.append(f"{icon} **{subject}** {stat} `{c['new_line']}` ({sport})")
            else:
                lines.append(f"{icon} **{subject}** {stat} `{c['old_line']}` removed ({sport})")

        if len(items) > 10:
            lines.append(f"*… and {len(items) - 10} more*")

        dominant = max(set(c["change_type"] for c in items), key=lambda t: sum(1 for x in items if x["change_type"] == t))
        embeds.append(_embed(
            title=f"{source.title()} Props Update",
            description="\n".join(lines),
            color=COLORS.get(dominant, 0xFDD835),
        ))

    # Discord allows max 10 embeds per message
    for i in range(0, len(embeds), 10):
        await _post({"embeds": embeds[i:i + 10]})


async def post_line_movement(movement: dict) -> None:
    embed = _embed(
        title=f"Line Movement: {movement.get('game', '')}",
        description=movement.get("description", ""),
        color=0xFDD835,
        fields=[
            {"name": "Type", "value": movement.get("move_type", "—")},
            {"name": "Move", "value": f"{movement.get('move_size', 0):+.1f}"},
        ],
    )
    await _post({"embeds": [embed]})


async def post_parlay(parlay: dict) -> None:
    legs = parlay.get("legs", [])
    desc = "\n".join(f"• {l.get('bet', 'Unknown')}" for l in legs)
    embed = _embed(
        title=f"Parlay ({len(legs)} legs) — {parlay.get('combined_odds', 0):+d}",
        description=desc,
        color=0xFDD835,
        fields=[
            {"name": "Combined EV", "value": f"{parlay.get('combined_ev', 0):.1%}"},
            {"name": "Units",       "value": str(parlay.get("units", 0))},
        ],
    )
    await _post({"embeds": [embed]})


async def post_daily_summary(summary_text: str) -> None:
    import zoneinfo
    from datetime import datetime
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    embed = _embed(
        title=f"📈  Daily Summary  ·  {et.strftime('%A, %B %-d')}",
        description=f"─────────────────────────\n{summary_text}",
        color=0x1565C0,
    )
    embed["footer"] = {"text": f"End of day recap · {et.strftime('%-I:%M %p ET')}"}
    await _post({"embeds": [embed]})


async def post_weekly_summary(summary_text: str) -> None:
    import zoneinfo
    from datetime import datetime
    et = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    embed = _embed(
        title=f"📅  Weekly Summary  ·  Week {et.isocalendar()[1]}",
        description=f"─────────────────────────\n{summary_text}",
        color=0x4A148C,
    )
    embed["footer"] = {"text": f"Sunday midnight recap · {et.strftime('%B %-d, %Y')}"}
    await _post({"embeds": [embed]})


async def post_game_alert(event: dict, minutes_remaining: float) -> None:
    from src.engines.timing_engine import get_urgency_level
    urgency = get_urgency_level(minutes_remaining)
    colors = {"critical": 0xC62828, "high": 0xF57F17, "medium": 0xFDD835, "low": 0x1E88E5}
    embed = _embed(
        title=f"Game Alert: {event.get('home_team', '')} vs {event.get('away_team', '')}",
        description=f"Starting in **{int(minutes_remaining)} minutes**",
        color=colors.get(urgency, 0x757575),
        fields=[
            {"name": "Sport",   "value": event.get("sport_key", "—")},
            {"name": "Urgency", "value": urgency.upper()},
        ],
    )
    await _post({"embeds": [embed]})


async def send_to_channel(channel_name: str, content: str = "", **kwargs) -> bool:
    """Compatibility shim — webhook has no channel routing, posts to configured URL."""
    payload: dict = {}
    if content:
        payload["content"] = content[:2000]
    embed = kwargs.get("embed")
    if embed and isinstance(embed, dict):
        payload["embeds"] = [embed]
    if not payload:
        return False
    return await _post(payload)
