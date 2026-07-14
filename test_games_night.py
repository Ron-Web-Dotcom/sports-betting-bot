"""
Run: python3 test_games_night.py
Shows today's NIGHT games (5 PM ET+) with odds + bot confidence.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from zoneinfo import ZoneInfo
from src.engines.odds_engine import get_latest_snapshots_by_game
from src.core.timezone import et_naive

ET    = ZoneInfo("America/New_York")
UTC   = ZoneInfo("UTC")
now   = et_naive()

# Always run a fresh scan so the table reflects live odds
print("  [Scanning today's games + live odds — please wait...]\n")
try:
    from src.workers.picks_worker import scan_todays_games
    scan_todays_games()
except Exception as _e:
    print(f"  [Sofascore scan warning: {_e}]")
from src.workers.odds_worker import scan_and_save_odds
scan_and_save_odds()

snaps = get_latest_snapshots_by_game()

# Build game list
games = {}
for game_id, snap_list in snaps.items():
    if not snap_list:
        continue
    s0 = snap_list[0]
    ct_str = s0.get("commence_time", "")
    if not ct_str:
        continue
    try:
        ct_utc = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        ct_et  = ct_utc.astimezone(ET)
    except Exception:
        continue

    # Night = 5 PM ET+
    if ct_et.hour < 17:
        continue
    # Today only
    if ct_et.date() != now.date():
        continue

    key = game_id
    if key not in games:
        games[key] = {
            "away":     s0.get("away_team", "?"),
            "home":     s0.get("home_team", "?"),
            "sport":    s0.get("sport_key", ""),
            "time_et":  ct_et.strftime("%-I:%M %p ET"),
            "h2h":      {},
        }

    # Collect best h2h odds per selection
    for s in snap_list:
        if s.get("market") != "h2h":
            continue
        sel  = s.get("selection", "")
        odds = s.get("best_odds")
        if sel and odds is not None:
            cur = games[key]["h2h"].get(sel)
            if cur is None or odds > cur:
                games[key]["h2h"][sel] = odds


def implied_prob(odds):
    if odds is None:
        return None
    if odds < 0:
        return round(abs(odds) / (abs(odds) + 100) * 100, 1)
    return round(100 / (100 + odds) * 100, 1)

def odds_fmt(odds):
    if odds is None:
        return "N/A"
    return f"+{odds}" if odds > 0 else str(odds)

def bot_pick(h2h):
    """Pick the - odds side (or least positive)."""
    if not h2h:
        return None, None
    neg = {s: o for s, o in h2h.items() if o < 0}
    if neg:
        sel = min(neg, key=lambda s: neg[s])
    else:
        sel = min(h2h, key=lambda s: h2h[s])
    return sel, h2h[sel]

def conf_label(prob):
    if prob is None:
        return "N/A"
    if prob >= 80:
        return f"{prob}% ✅"
    if prob >= 70:
        return f"{prob}% ⚠️"
    return f"{prob}% ❌"

# Print table
COL = [28, 10, 10, 12, 10, 12, 14]
HDR = ["MATCHUP", "TIME ET", "SPORT", "BOT PICK", "ODDS", "IMPLIED", "CONFIDENCE"]

def row_fmt(cols):
    return "  ".join(str(c).ljust(w) for c, w in zip(cols, COL))

sep = "-" * (sum(COL) + 2 * len(COL))

print(f"\n{'='*80}")
print(f"  NIGHT GAMES — {now.strftime('%b %d, %Y  %I:%M %p ET')}  ({len(games)} night / {len(snaps)} total in DB)")
print(f"{'='*80}")
print(row_fmt(HDR))
print(sep)

if not games and snaps:
    print("  No night games (5 PM ET+) today — check day games with test_games_day.py")
elif not games:
    print("  No games found in DB. Run a scan first.")
else:
    for g in sorted(games.values(), key=lambda x: x["time_et"]):
        matchup  = f"{g['away']} @ {g['home']}"[:28]
        pick_sel, pick_odds = bot_pick(g["h2h"])
        prob     = implied_prob(pick_odds)
        sport    = g["sport"].replace("basketball_", "").replace("soccer_", "")[:10]
        print(row_fmt([
            matchup,
            g["time_et"],
            sport,
            (pick_sel or "?")[:12],
            odds_fmt(pick_odds),
            f"{prob}%" if prob else "N/A",
            conf_label(prob),
        ]))

print(sep)
print(f"\n  ✅ = 80%+  ⚠️ = 70-79%  ❌ = <70%")
print(f"  Confidence based on market implied probability of bot's selected side.\n")
