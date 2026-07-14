"""
Run: python3 test_games.py
Shows all today's games + odds currently stored in the DB.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from src.engines.odds_engine import get_latest_snapshots_by_game
from src.core.timezone import et_naive

now   = et_naive()
snaps = get_latest_snapshots_by_game()

games = {}
for snap_list in (snaps.values() if isinstance(snaps, dict) else [snaps]):
    if not isinstance(snap_list, list):
        snap_list = [snap_list]
    for s in snap_list:
        key = f"{s.get('away_team','?')} @ {s.get('home_team','?')}"
        if key not in games:
            games[key] = {
                "sport":    s.get("sport_key", ""),
                "commence": s.get("commence_time", ""),
                "markets":  {},
            }
        mkt = s.get("market", "h2h")
        sel = s.get("selection", "")
        odds = s.get("best_odds")
        if mkt not in games[key]["markets"]:
            games[key]["markets"][mkt] = {}
        if sel:
            games[key]["markets"][mkt][sel] = odds

print(f"\n{'='*60}")
print(f"  TODAY'S GAMES — {now.strftime('%b %d, %Y  %I:%M %p ET')}")
print(f"  Total: {len(games)} games")
print(f"{'='*60}\n")

for matchup, info in games.items():
    print(f"  {matchup}")
    print(f"  Sport:    {info['sport']}")
    print(f"  Kickoff:  {info['commence']}")
    for mkt, sides in info["markets"].items():
        odds_str = "  |  ".join(f"{sel}: {o}" for sel, o in sides.items() if sel)
        print(f"  [{mkt}]  {odds_str}")
    print()
