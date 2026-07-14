"""
Run: python3 test_kalshi_night.py
Shows Kalshi sports markets closing 5 PM ET+ today.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from zoneinfo import ZoneInfo
from src.apis.kalshi import get_sports_markets
from src.core.timezone import et_naive

ET  = ZoneInfo("America/New_York")
now = et_naive()

all_markets = get_sports_markets()

# Filter: close_time 5 PM ET+ today
markets = []
for m in all_markets:
    ct_str = m.get("close_time") or m.get("expiration_time") or ""
    if not ct_str:
        continue
    try:
        ct_utc = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        ct_et  = ct_utc.astimezone(ET)
    except Exception:
        continue
    if ct_et.date() == now.date() and ct_et.hour >= 17:
        markets.append(m)

COL = [36, 8, 8, 8, 10, 16]
HDR = ["TITLE", "YES¢", "NO¢", "PICK", "VOLUME", "CLOSE ET"]

def row_fmt(cols):
    return "  ".join(str(c).ljust(w) for c, w in zip(cols, COL))

sep = "-" * (sum(COL) + 2 * len(COL))

print(f"\n{'='*80}")
print(f"  KALSHI NIGHT MARKETS — {now.strftime('%b %d, %Y  %I:%M %p ET')}  ({len(markets)} markets 5 PM ET+)")
print(f"{'='*80}")
print(row_fmt(HDR))
print(sep)

if not markets:
    print("  No night markets found.")
else:
    for m in sorted(markets, key=lambda x: x.get("close_time") or ""):
        yes_p = m.get("yes_price") or 0
        no_p  = m.get("no_price")  or 0
        pick  = "YES" if yes_p >= no_p else "NO"
        title = (m.get("title") or "")[:36]
        vol   = m.get("volume") or 0

        ct_str = m.get("close_time") or m.get("expiration_time") or ""
        close_et = ""
        if ct_str:
            try:
                ct_utc   = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                close_et = ct_utc.astimezone(ET).strftime("%-I:%M %p ET")
            except Exception:
                pass

        print(row_fmt([title, yes_p, no_p, pick, f"${vol:,}", close_et]))

print(sep)
print(f"\n  PICK = higher-probability side (YES if yes¢ ≥ no¢, else NO)\n")
