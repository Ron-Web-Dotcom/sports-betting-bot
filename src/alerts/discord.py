"""Discord webhook alerts — all notification types."""
import logging
from datetime import datetime
import httpx
from config.settings import DISCORD_WEBHOOK_URL, PAPER_TRADING

logger = logging.getLogger(__name__)

_BADGE = "PAPER" if PAPER_TRADING else "LIVE"
_TS    = lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def _post(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        resp = httpx.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("Discord error: %s", exc)
        return False


def _embed(title: str, color: int, fields: list, description: str = "") -> dict:
    e = {
        "title":  title,
        "color":  color,
        "fields": fields,
        "footer": {"text": "Sports Betting Bot | " + _TS()},
    }
    if description:
        e["description"] = description
    return e


# ── Straight bet ──────────────────────────────────────────────────────────────

def alert_bet_placed(
    *, event_name, selection, market, american_odds, stake,
    edge, confidence, book, reasoning, key_factors, signal_type="value",
) -> None:
    sign   = "+" if american_odds > 0 else ""
    colors = {"steam": 0xFF8800, "sharp": 0x00CCFF, "fade": 0xFF3366, "value": 0xFFD700}
    color  = colors.get(signal_type, 0xFFD700)
    fields = [
        {"name": "Event",       "value": event_name,                             "inline": False},
        {"name": "Selection",   "value": selection,                              "inline": True},
        {"name": "Market",      "value": market,                                 "inline": True},
        {"name": "Odds",        "value": f"{sign}{american_odds}",               "inline": True},
        {"name": "Stake",       "value": f"${stake:.2f}",                        "inline": True},
        {"name": "Edge",        "value": f"{edge:.1%}",                          "inline": True},
        {"name": "Confidence",  "value": f"{confidence:.1%}",                    "inline": True},
        {"name": "Book",        "value": book.replace("_", " ").title(),         "inline": True},
        {"name": "Signal",      "value": signal_type.upper(),                    "inline": True},
        {"name": "Reasoning",   "value": reasoning,                              "inline": False},
        {"name": "Key Factors", "value": "\n".join("- " + f for f in key_factors), "inline": False},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] Straight Bet", color, fields)]})


def alert_parlay_placed(candidate, parlay_id: int) -> None:
    legs_text = "\n".join(
        f"- {s.selection} ({s.event_name}) @ {s.american_odds:+d} ({s.book})"
        for s in candidate.legs
    )
    sign   = "+" if candidate.american_odds > 0 else ""
    payout = round(candidate.sizing["stake"] * candidate.combined_decimal, 2)
    fields = [
        {"name": "Legs",      "value": legs_text,                               "inline": False},
        {"name": "Combined",  "value": f"{sign}{candidate.american_odds}",      "inline": True},
        {"name": "Stake",     "value": f"${candidate.sizing['stake']:.2f}",     "inline": True},
        {"name": "Payout",    "value": f"${payout:.2f}",                        "inline": True},
        {"name": "EV",        "value": f"{candidate.ev:.1%}",                   "inline": True},
        {"name": "Win Prob",  "value": f"{candidate.win_probability:.1%}",      "inline": True},
        {"name": "Type",      "value": candidate.parlay_type.upper(),           "inline": True},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] {len(candidate.legs)}-Leg Parlay #{parlay_id}", 0x9B59B6, fields)]})


def alert_bet_settled(*, event_name, selection, result, pnl, stake, new_bankroll) -> None:
    won    = result == "win"
    color  = 0x00CC44 if won else 0xFF3333
    label  = "WON" if won else "LOST"
    fields = [
        {"name": "Event",        "value": event_name,            "inline": False},
        {"name": "Selection",    "value": selection,             "inline": True},
        {"name": "Stake",        "value": f"${stake:.2f}",       "inline": True},
        {"name": "P&L",          "value": f"${pnl:+.2f}",        "inline": True},
        {"name": "New Bankroll", "value": f"${new_bankroll:.2f}", "inline": True},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] {label}", color, fields)]})


def alert_parlay_settled(*, parlay_id, legs_summary, result, pnl, stake, new_bankroll) -> None:
    won    = result == "win"
    color  = 0x00CC44 if won else 0xFF3333
    label  = "PARLAY HIT" if won else "PARLAY LOST"
    fields = [
        {"name": "Legs",         "value": legs_summary,          "inline": False},
        {"name": "Stake",        "value": f"${stake:.2f}",       "inline": True},
        {"name": "P&L",          "value": f"${pnl:+.2f}",        "inline": True},
        {"name": "New Bankroll", "value": f"${new_bankroll:.2f}", "inline": True},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] {label} #{parlay_id}", color, fields)]})


# ── Arb alert ─────────────────────────────────────────────────────────────────

def alert_arb(arb) -> None:
    fields = [
        {"name": "Event",  "value": arb.event_name,  "inline": False},
        {"name": "Market", "value": arb.market,      "inline": True},
        {"name": "Profit", "value": f"{arb.guaranteed_profit_pct:.2%} guaranteed on $100", "inline": True},
        {"name": "Side A", "value": f"{arb.book_a}: {arb.selection_a} @ {arb.american_odds_a:+d} | ${arb.stake_a:.2f}", "inline": False},
        {"name": "Side B", "value": f"{arb.book_b}: {arb.selection_b} @ {arb.american_odds_b:+d} | ${arb.stake_b:.2f}", "inline": False},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] ARB {arb.guaranteed_profit_pct:.2%}", 0x00FFCC, fields)]})


# ── Hedge alert ───────────────────────────────────────────────────────────────

def alert_hedge(rec) -> None:
    sign   = "+" if rec.hedge_american_odds > 0 else ""
    fields = [
        {"name": "Parlay Status",    "value": rec.trigger_summary,                                                       "inline": True},
        {"name": "Potential Payout", "value": f"${rec.potential_payout:.2f}",                                            "inline": True},
        {"name": "Hedge On",         "value": f"{rec.hedge_selection} @ {sign}{rec.hedge_american_odds}",                "inline": False},
        {"name": "Full Hedge",       "value": f"Stake ${rec.full_hedge_stake:.2f} locks ${rec.full_hedge_profit:.2f}",   "inline": False},
        {"name": "Partial Hedge",    "value": f"Stake ${rec.partial_hedge_stake:.2f} | up: ${rec.partial_hedge_upside:.2f} | floor: ${rec.partial_hedge_floor:.2f}", "inline": False},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] HEDGE — Parlay #{rec.parlay_id}", 0xFF8800, fields)]})


# ── Session summary ───────────────────────────────────────────────────────────

def alert_session_summary(summary_text: str, stats: dict, arb_count: int = 0) -> None:
    total = stats.get("total", 0)
    wins  = stats.get("wins",  0)
    pnl   = stats.get("total_pnl", 0.0) or 0.0
    pct   = f"{wins/total*100:.1f}%" if total else "n/a"
    fields = [
        {"name": "Bets",       "value": str(total),    "inline": True},
        {"name": "Win %",      "value": pct,           "inline": True},
        {"name": "Total P&L",  "value": f"${pnl:+.2f}", "inline": True},
        {"name": "Arbs Found", "value": str(arb_count), "inline": True},
    ]
    _post({"embeds": [_embed(f"[{_BADGE}] Session Summary", 0x5865F2, fields, description=summary_text)]})


# ── System alerts ─────────────────────────────────────────────────────────────

def alert_startup(bankroll: float) -> None:
    fields = [
        {"name": "Bankroll", "value": f"${bankroll:.2f}",             "inline": True},
        {"name": "Strategy", "value": "Parlays + Straight Bets + Arb", "inline": True},
    ]
    _post({"embeds": [_embed(f"Sports Betting Bot Online [{_BADGE}]", 0x5865F2, fields)]})


def alert_error(message: str) -> None:
    _post({"embeds": [_embed("Bot Error", 0xFF8800, [], description=message)]})
