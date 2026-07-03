"""
Quick diagnostic: check Redis Sofascore cache + sample Kalshi markets + test matching.
Run: python3 debug_kalshi.py
"""
import json, os, sys
sys.path.insert(0, "/root/sports-bot")
os.chdir("/root/sports-bot")

import redis
from src.core.config import REDIS_URL

r = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

# 1. Sofascore cache
idx_raw = r.get("sofascore:today_index")
day_raw = r.get("sofascore:day_games")
ngt_raw = r.get("sofascore:night_games")

print("=== SOFASCORE CACHE ===")
print(f"today_index: {len(json.loads(idx_raw)) if idx_raw else 'EMPTY'} teams")
print(f"day_games:   {len(json.loads(day_raw)) if day_raw else 'EMPTY'} games")
print(f"night_games: {len(json.loads(ngt_raw)) if ngt_raw else 'EMPTY'} games")

if idx_raw:
    idx = json.loads(idx_raw)
    print("\nSample Sofascore teams (first 10):")
    for i, (k, v) in enumerate(list(idx.items())[:10]):
        print(f"  '{k}' → {v.get('home_team')} vs {v.get('away_team')} @ {v.get('commence_time')}")

# 2. Kalshi markets from cache
kalshi_raw = r.get("kalshi:live_markets")
print(f"\n=== KALSHI CACHE ===")
if kalshi_raw:
    markets = json.loads(kalshi_raw)
    print(f"{len(markets)} markets cached")
    print("\nSample Kalshi subtitles (first 15):")
    for m in markets[:15]:
        print(f"  subtitle='{m.get('subtitle')}' | title='{m.get('title', '')[:60]}'")
else:
    print("EMPTY — fetching live...")
    from src.apis.kalshi import get_sports_events
    markets = get_sports_events()
    print(f"{len(markets)} markets fetched")
    print("\nSample Kalshi subtitles (first 15):")
    for m in markets[:15]:
        print(f"  subtitle='{m.get('subtitle')}' | title='{m.get('title', '')[:60]}'")

# 3. Try matching
if idx_raw and markets:
    idx = json.loads(idx_raw)
    sf_games = list({v["id"]: v for v in idx.values() if v.get("id")}.values())
    print(f"\n=== MATCHING TEST ({len(sf_games)} sf_games vs {len(markets)} kalshi markets) ===")
    matched = 0
    unmatched = []
    for m in markets[:50]:
        subtitle = (m.get("subtitle") or "").lower()
        if not subtitle:
            continue
        found = False
        for g in sf_games:
            home = (g.get("home_team") or "").lower()
            away = (g.get("away_team") or "").lower()
            if home in subtitle or away in subtitle:
                found = True
                matched += 1
                break
        if not found:
            unmatched.append(m.get("subtitle", ""))

    print(f"Matched: {matched} | Unmatched: {len(unmatched)}")
    print("\nUnmatched subtitles (first 10):")
    for s in unmatched[:10]:
        print(f"  '{s}'")
