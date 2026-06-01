"""Discord webhook alerts — plain-English bet notifications."""
import logging
from datetime import datetime

import httpx

from config.settings import DISCORD_WEBHOOK_URL, PAPER_TRADING

logger = logging.getLogger(__name__)


def _post(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — skipping alert")
        return False
    try:
        resp = httpx.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("Discord webhook error: %s", exc)
        return False


def _mode_badge() -> str:
    return "🟡 PAPER" if PAPER_TRADING else "🔴 LIVE"


def alert_bet_placed(
    *,
    event_name: str,
    selection: str,
    market: str,
    american_odds: int,
    stake: float,
    edge: float,
    confidence: float,
    platform: str,
    reasoning: str,
    key_factors: list[str],
) -> None:
    sign = "+" if american_odds > 0 else ""
    embed = {
        "title": f"{_mode_badge()} New Bet Placed",
        "color": 0xFFD700 if PAPER_TRADING else 0xFF4444,
        "fields": [
            {"name": "Event",      "value": event_name,                     "inline": False},
            {"name": "Selection",  "value": selection,                      "inline": True},
            {"name": "Market",     "value": market,                         "inline": True},
            {"name": "Odds",       "value": f"{sign}{american_odds}",       "inline": True},
            {"name": "Stake",      "value": f"${stake:.2f}",                "inline": True},
            {"name": "Edge",       "value": f"{edge:.1%}",                  "inline": True},
            {"name": "Confidence", "value": f"{confidence:.1%}",            "inline": True},
            {"name": "Platform",   "value": platform.title(),               "inline": True},
            {"name": "Reasoning",  "value": reasoning,                      "inline": False},
            {"name": "Key Factors","value": "\n".join(f"• {f}" for f in key_factors), "inline": False},
        ],
        "footer": {"text": f"Sports Betting Bot • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    _post({"embeds": [embed]})


def alert_bet_settled(
    *,
    event_name: str,
    selection: str,
    result: str,
    pnl: float,
    stake: float,
    new_bankroll: float,
) -> None:
    won = result == "win"
    embed = {
        "title": f"{'✅ Won' if won else '❌ Lost'} — {_mode_badge()}",
        "color": 0x00CC44 if won else 0xFF3333,
        "fields": [
            {"name": "Event",        "value": event_name,          "inline": False},
            {"name": "Selection",    "value": selection,           "inline": True},
            {"name": "Result",       "value": result.upper(),      "inline": True},
            {"name": "Stake",        "value": f"${stake:.2f}",     "inline": True},
            {"name": "P&L",          "value": f"${pnl:+.2f}",      "inline": True},
            {"name": "New Bankroll", "value": f"${new_bankroll:.2f}", "inline": True},
        ],
        "footer": {"text": f"Sports Betting Bot • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    _post({"embeds": [embed]})


def alert_session_summary(summary_text: str, stats: dict) -> None:
    total   = stats.get("total", 0)
    wins    = stats.get("wins", 0)
    losses  = stats.get("losses", 0)
    pnl     = stats.get("total_pnl", 0.0)
    win_pct = (wins / total * 100) if total else 0

    embed = {
        "title": f"📊 Session Summary — {_mode_badge()}",
        "color": 0x5865F2,
        "description": summary_text,
        "fields": [
            {"name": "Total Bets", "value": str(total),          "inline": True},
            {"name": "W/L",        "value": f"{wins}/{losses}",  "inline": True},
            {"name": "Win %",      "value": f"{win_pct:.1f}%",   "inline": True},
            {"name": "Total P&L",  "value": f"${pnl:+.2f}",      "inline": True},
        ],
        "footer": {"text": f"Sports Betting Bot • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    _post({"embeds": [embed]})


def alert_error(message: str) -> None:
    _post({
        "embeds": [{
            "title": "⚠️ Bot Error",
            "color": 0xFF8800,
            "description": message,
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def alert_startup(bankroll: float) -> None:
    _post({
        "embeds": [{
            "title": f"🚀 Sports Betting Bot Started — {_mode_badge()}",
            "color": 0x5865F2,
            "fields": [
                {"name": "Bankroll", "value": f"${bankroll:.2f}", "inline": True},
            ],
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })
