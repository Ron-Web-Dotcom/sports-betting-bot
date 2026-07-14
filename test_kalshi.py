"""
Run: python3 test_kalshi.py
Shows all active Kalshi sports markets right now.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from src.apis.kalshi import get_sports_markets
from src.core.timezone import et_naive

now     = et_naive()
markets = get_sports_markets()

print(f"\n{'='*60}")
print(f"  KALSHI MARKETS — {now.strftime('%b %d, %Y  %I:%M %p ET')}")
print(f"  Total: {len(markets)} markets")
print(f"{'='*60}\n")

for m in markets:
    yes = m.get("yes_price", "?")
    no  = m.get("no_price",  "?")
    print(f"  {m.get('title','')}")
    print(f"  Ticker: {m.get('ticker','')}  |  YES: {yes}¢  NO: {no}¢")
    print(f"  Sport:  {m.get('sport_key','')}  |  Volume: ${m.get('volume',0):,}")
    print()
