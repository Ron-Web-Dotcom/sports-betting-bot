"""
Print full W/L history from SQLite — zero impact on Redis or any running process.

Usage:
    python scripts/show_history.py              # all-time list
    python scripts/show_history.py week         # this week only
    python scripts/show_history.py 2026-07-06  # specific week (any Monday date)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.workers.slip_tracker import get_tracklist, get_weekly_tracklist, _get_week_start

def _icon(result): return "✅ CASHED" if result == "cashed" else "❌  DEAD "

def print_alltime():
    slips = get_tracklist(limit=500)
    wins   = sum(1 for s in slips if s["result"] == "cashed")
    losses = sum(1 for s in slips if s["result"] == "dead")
    total  = wins + losses
    pct    = f"  ({round(wins/total*100)}% win rate)" if total else ""
    print(f"\n{'━'*60}")
    print(f"  ALL-TIME RECORD: {wins}W – {losses}L{pct}")
    print(f"  Total settled: {total}")
    print(f"{'━'*60}")
    if not slips:
        print("  No settled slips yet.")
        return
    print(f"\n  {'#':<4} {'DATE':<12} {'PLAT':<8} {'PD':<5} {'RESULT':<10}  PICKS")
    print(f"  {'─'*4} {'─'*12} {'─'*8} {'─'*5} {'─'*10}  {'─'*30}")
    for i, s in enumerate(reversed(slips), 1):
        plat = s["platform"].upper()[:4]
        pd   = s["period"].upper()[0] if s["period"] else "?"
        picks_short = s["pick_names"][:45] + ("…" if len(s["pick_names"]) > 45 else "")
        print(f"  {i:<4} {s['date']:<12} {plat:<8} {pd:<5} {_icon(s['result'])}  {picks_short}")
    print()

def print_week(week_start=None):
    data = get_weekly_tracklist(week_start)
    ws   = data["week_start"]
    w, l = data["wins"], data["losses"]
    total = w + l
    pct = f"  ({round(w/total*100)}% win rate)" if total else ""
    print(f"\n{'━'*60}")
    print(f"  WEEK OF {ws}: {w}W – {l}L{pct}")
    print(f"  Total settled: {total}")
    print(f"{'━'*60}")
    if not data["slips"]:
        print("  No settled slips this week.")
        return
    print(f"\n  {'DATE':<12} {'PLAT':<8} {'PD':<5} {'RESULT':<10}  PICKS")
    print(f"  {'─'*12} {'─'*8} {'─'*5} {'─'*10}  {'─'*30}")
    for s in data["slips"]:
        plat = s["platform"].upper()[:4]
        pd   = s["period"].upper()[0] if s["period"] else "?"
        picks_short = s["pick_names"][:45] + ("…" if len(s["pick_names"]) > 45 else "")
        print(f"  {s['date']:<12} {plat:<8} {pd:<5} {_icon(s['result'])}  {picks_short}")
    print()

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "week":
        print_week(_get_week_start())
    elif arg and arg != "all":
        print_week(arg)  # specific Monday date
    else:
        print_alltime()
