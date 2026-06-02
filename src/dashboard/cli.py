"""
Live CLI dashboard — Kalshi-style terminal UI.
Shows portfolio, open positions/parlays, recent signals, arbs, bankroll chart.
"""
import sys
import time
import threading
from datetime import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
    from rich.live import Live
    from rich.layout import Layout
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from src.core.database import (
    get_open_positions, get_open_parlays, get_top_signals,
    get_recent_arbs, latest_bankroll, get_position_stats,
)
from config.settings import PAPER_TRADING

console = Console() if RICH_AVAILABLE else None


def _mode_str() -> str:
    return "[yellow]PAPER[/yellow]" if PAPER_TRADING else "[red bold]LIVE[/red bold]"


def _pnl_color(val: float) -> str:
    if val > 0:
        return f"[green]+${val:.2f}[/green]"
    if val < 0:
        return f"[red]-${abs(val):.2f}[/red]"
    return f"$0.00"


def build_header() -> Panel:
    bankroll = latest_bankroll() or 0.0
    stats    = get_position_stats()
    pnl      = stats.get("total_pnl", 0.0) or 0.0
    wins     = stats.get("wins",  0) or 0
    total    = stats.get("total", 0) or 0
    pct      = f"{wins/total*100:.1f}%" if total else "—"

    text = (
        f" [bold cyan]Sports Betting Bot[/bold cyan]  {_mode_str()}   "
        f"[bold]Bankroll:[/bold] ${bankroll:.2f}   "
        f"[bold]P&L:[/bold] {_pnl_color(pnl)}   "
        f"[bold]Win:[/bold] {pct} ({wins}/{total})   "
        f"[dim]{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}[/dim]"
    )
    return Panel(Text.from_markup(text), box=box.HORIZONTALS)


def build_positions_table() -> Table:
    positions = get_open_positions()
    t = Table(title="Open Positions", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("#",         style="dim",    width=4)
    t.add_column("Event",                     width=30)
    t.add_column("Selection",                 width=20)
    t.add_column("Market",    style="cyan",   width=10)
    t.add_column("Odds",      style="yellow", width=7)
    t.add_column("Stake",     style="green",  width=8)
    t.add_column("Edge",      style="magenta",width=7)
    t.add_column("Book",      style="dim",    width=14)

    for p in positions[:15]:
        sign = "+" if p["american_odds"] > 0 else ""
        t.add_row(
            str(p["id"]),
            p["event_name"][:29],
            p["selection"][:19],
            p["market"],
            f"{sign}{p['american_odds']}",
            f"${p['stake']:.2f}",
            f"{p['edge']:.1%}",
            p["book"][:13],
        )
    if not positions:
        t.add_row("—", "No open positions", "", "", "", "", "", "")
    return t


def build_parlays_table() -> Table:
    parlays = get_open_parlays()
    t = Table(title="Open Parlays", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("#",       style="dim",    width=4)
    t.add_column("Type",    style="cyan",   width=10)
    t.add_column("Legs",    style="yellow", width=5)
    t.add_column("Won",     style="green",  width=5)
    t.add_column("Odds",    style="yellow", width=8)
    t.add_column("Stake",   style="green",  width=8)
    t.add_column("Payout",  style="green",  width=10)

    for p in parlays[:8]:
        from src.apis.odds_api import american_to_decimal
        dec = p["combined_odds"]
        am  = int((dec - 1) * 100) if dec >= 2 else int(-100 / (dec - 1))
        sign = "+" if am > 0 else ""
        t.add_row(
            str(p["id"]),
            p["parlay_type"].upper()[:9],
            str(p["leg_count"]),
            str(p["legs_won"]),
            f"{sign}{am}",
            f"${p['stake']:.2f}",
            f"${p['potential_payout']:.2f}",
        )
    if not parlays:
        t.add_row("—", "No open parlays", "", "", "", "", "")
    return t


def build_signals_table() -> Table:
    signals = get_top_signals(limit=10)
    t = Table(title="Top Signals (last hour)", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Score",   style="bold yellow", width=7)
    t.add_column("Event",                        width=28)
    t.add_column("Selection",                    width=18)
    t.add_column("Type",    style="cyan",         width=10)
    t.add_column("AI Conf", style="green",        width=8)
    t.add_column("Edge",    style="magenta",      width=7)

    for s in signals:
        score_color = "green" if s["composite_score"] > 0.65 else "yellow" if s["composite_score"] > 0.55 else "red"
        t.add_row(
            f"[{score_color}]{s['composite_score']:.3f}[/{score_color}]",
            s["event_name"][:27],
            s["selection"][:17],
            s["signal_type"].upper()[:9],
            f"{s['ai_confidence']:.1%}",
            f"{s['composite_score'] - 0.5:.1%}",
        )
    if not signals:
        t.add_row("—", "No signals yet", "", "", "", "")
    return t


def build_arbs_table() -> Table:
    arbs = get_recent_arbs(hours=1)
    t = Table(title="Recent Arbs (1h)", box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Profit%", style="bold green", width=8)
    t.add_column("Event",                        width=28)
    t.add_column("Market",  style="cyan",         width=8)
    t.add_column("Book A",                        width=14)
    t.add_column("Book B",                        width=14)

    for a in arbs[:6]:
        t.add_row(
            f"{a['guaranteed_profit_pct']:.2%}",
            a["event_name"][:27],
            a["market"],
            f"{a['book_a']} {a['odds_a']:+d}",
            f"{a['book_b']} {a['odds_b']:+d}",
        )
    if not arbs:
        t.add_row("—", "No arbs found", "", "", "")
    return t


def render_once() -> None:
    if not RICH_AVAILABLE:
        print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Dashboard requires 'rich' — pip install rich")
        return
    console.print(build_header())
    console.print(Columns([build_positions_table(), build_parlays_table()]))
    console.print(Columns([build_signals_table(), build_arbs_table()]))


def run_live(refresh_seconds: int = 5) -> None:
    if not RICH_AVAILABLE:
        print("Dashboard requires 'rich' — pip install rich")
        return

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="bottom"),
    )
    layout["main"].split_row(Layout(name="positions"), Layout(name="parlays"))
    layout["bottom"].split_row(Layout(name="signals"),  Layout(name="arbs"))

    with Live(layout, refresh_per_second=1, screen=True):
        while True:
            layout["header"].update(build_header())
            layout["positions"].update(Panel(build_positions_table(), border_style="blue"))
            layout["parlays"].update(Panel(build_parlays_table(),   border_style="purple"))
            layout["signals"].update(Panel(build_signals_table(),   border_style="yellow"))
            layout["arbs"].update(Panel(build_arbs_table(),         border_style="green"))
            time.sleep(refresh_seconds)
