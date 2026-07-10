#!/usr/bin/env python3
"""Check all data feeds: Sofascore, Odds API, Kalshi markets."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# ── Sofascore ──────────────────────────────────────────────────────────────────
print("=== Sofascore ===")
try:
    from src.apis.sofascore import get_todays_events
    events = get_todays_events()
    if events:
        print(f"OK — {len(events)} events today")
        for e in events[:5]:
            print(f"  {e.get('home_team')} vs {e.get('away_team')}  [{e.get('sport','?')}]  {e.get('start_time_et','')}")
    else:
        print("WARN — 0 events returned (may be no games today or proxy issue)")
except Exception as ex:
    print(f"FAIL — {ex}")

# ── Odds API ───────────────────────────────────────────────────────────────────
print("\n=== Odds API ===")
try:
    from src.engines.odds_engine import get_latest_snapshots_by_game
    snaps = get_latest_snapshots_by_game()
    if snaps:
        print(f"OK — {len(snaps)} games with odds")
        for gid, s in list(snaps.items())[:5]:
            first = s[0] if s else {}
            print(f"  {first.get('home_team')} vs {first.get('away_team')}  [{first.get('sport_key','?')}]")
    else:
        print("WARN — 0 games returned")
except Exception as ex:
    print(f"FAIL — {ex}")

# ── Kalshi Markets ─────────────────────────────────────────────────────────────
print("\n=== Kalshi Markets ===")
try:
    from src.apis.kalshi import get_sports_events
    markets = get_sports_events()
    if markets:
        print(f"OK — {len(markets)} sport events")
        for m in markets[:5]:
            print(f"  {m.get('title','?')}  closes={m.get('close_time','?')[:10]}")
    else:
        print("WARN — 0 markets returned")
except Exception as ex:
    print(f"FAIL — {ex}")

print("\nDone.")
