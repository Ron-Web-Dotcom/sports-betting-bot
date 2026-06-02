"""
Discord integration — webhook-only.

All alerts are sent as HTTP POST to DISCORD_WEBHOOK_URL.
No bot token, no slash commands, no discord.py bot process needed.
"""
import json
import logging
import httpx
from src.core.config import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds


# ── Core send ──────────────────────────────────────────────────────────────────

async def _post(payload: dict) -> bool:
    """POST payload to the webhook URL. Returns True on success."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — alert suppressed")
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 204:
                return True
            logger.error("Webhook POST failed: %s %s", resp.status_code, resp.text[:200])
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
    desc = "\n".join(f"• {l.get('bet', l)}" for l in legs)
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
    embed = _embed(title="Daily Summary", description=summary_text, color=0x1565C0)
    await _post({"embeds": [embed]})


async def post_weekly_summary(summary_text: str) -> None:
    embed = _embed(title="Weekly Summary", description=summary_text, color=0x4A148C)
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
