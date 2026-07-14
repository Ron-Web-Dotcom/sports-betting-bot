"""
Run: python3 getlive_gamestoday.py

Reads data already in DB/cache — no API requests made.
Shows 3 tables:
  1. Today's games (all sports, men + women) with odds & AI confidence
  2. Kalshi markets today
  3. Sofascore games today
"""
import os, sys, logging
sys.path.insert(0, os.path.dirname(__file__))
logging.disable(logging.CRITICAL)

from datetime import datetime
from zoneinfo import ZoneInfo
from src.core.timezone import et_naive

ET  = ZoneInfo("America/New_York")
now = et_naive()

# ── HELPERS ───────────────────────────────────────────────────────────────────
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
    if not h2h:
        return None, None
    neg = {s: o for s, o in h2h.items() if o < 0}
    sel = min(neg, key=lambda s: neg[s]) if neg else min(h2h, key=lambda s: h2h[s])
    return sel, h2h[sel]

def conf_label(prob):
    if prob is None:
        return "N/A"
    if prob >= 80:
        return f"{prob}%  ✅"
    if prob >= 70:
        return f"{prob}%  ⚠️"
    return f"{prob}%  ❌"

def row_fmt(cols, widths):
    return "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))

def parse_et(ct_str):
    if not ct_str:
        return None
    try:
        dt = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None

print(f"\n{'='*80}")
print(f"  LIVE GAMES TODAY  —  {now.strftime('%b %d, %Y  %I:%M %p ET')}")
print(f"{'='*80}")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 1 — TODAY'S GAMES from DB
# ═══════════════════════════════════════════════════════════════════════════════
from src.engines.odds_engine import get_latest_snapshots_by_game
snaps = get_latest_snapshots_by_game()

games = {}
for game_id, snap_list in snaps.items():
    if not snap_list:
        continue
    s0 = snap_list[0]
    ct_et = parse_et(s0.get("commence_time", ""))
    if not ct_et or ct_et.date() != now.date():
        continue
    if game_id not in games:
        games[game_id] = {
            "away":    s0.get("away_team", "?"),
            "home":    s0.get("home_team", "?"),
            "sport":   (s0.get("sport_key", "")
                        .replace("basketball_", "").replace("soccer_", "")
                        .replace("americanfootball_", "").replace("icehockey_", "")
                        .replace("baseball_", ""))[:10],
            "time_et": ct_et.strftime("%-I:%M %p"),
            "period":  "NIGHT" if ct_et.hour >= 17 else "DAY",
            "sort_dt": ct_et,
            "h2h":     {},
        }
    for s in snap_list:
        if s.get("market") != "h2h":
            continue
        sel, odds = s.get("selection", ""), s.get("best_odds")
        if sel and odds is not None:
            cur = games[game_id]["h2h"].get(sel)
            if cur is None or odds > cur:
                games[game_id]["h2h"][sel] = odds

G_COL = [30, 9, 10, 9, 14, 10, 14]
G_HDR = ["MATCHUP", "TIME ET", "SPORT", "PERIOD", "BOT PICK", "ODDS", "CONFIDENCE"]
sep_g = "-" * (sum(G_COL) + 2 * len(G_COL))

print(f"\n  TODAY'S GAMES  ({len(games)} games — men + women, all sports)")
print(row_fmt(G_HDR, G_COL))
print(sep_g)

if not games:
    print("  No games in DB yet — bot scans at 8 AM, 10 AM, 3 PM ET.")
else:
    for g in sorted(games.values(), key=lambda x: x["sort_dt"]):
        matchup = f"{g['away']} @ {g['home']}"[:30]
        pick_sel, pick_odds = bot_pick(g["h2h"])
        prob = implied_prob(pick_odds)
        print(row_fmt([
            matchup, g["time_et"], g["sport"], g["period"],
            (pick_sel or "?")[:14], odds_fmt(pick_odds), conf_label(prob),
        ], G_COL))

print(sep_g)
print("  ✅ 80%+  ⚠️ 70-79%  ❌ <70%  |  Confidence = market implied prob")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 2 — KALSHI MARKETS from cache
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import redis as _r2, json as _j2
    from src.core.config import REDIS_URL
    _rc = _r2.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    _raw_k = _rc.get("kalshi:live_markets") or "[]"
    all_kalshi = _j2.loads(_raw_k)
except Exception:
    all_kalshi = []

if not all_kalshi:
    try:
        from src.apis.kalshi import get_sports_markets
        all_kalshi = get_sports_markets()
    except Exception:
        all_kalshi = []

k_today = []
for m in all_kalshi:
    ct_str = m.get("close_time") or m.get("expiration_time") or ""
    ct_et  = parse_et(ct_str)
    if ct_et and ct_et.date() == now.date():
        m["_close_et"]  = ct_et
        m["_close_str"] = ct_et.strftime("%-I:%M %p")
        m["_period"]    = "NIGHT" if ct_et.hour >= 17 else "DAY"
        k_today.append(m)
    elif not ct_str:
        m["_close_et"]  = None
        m["_close_str"] = "—"
        m["_period"]    = "—"
        k_today.append(m)

K_COL = [38, 6, 6, 6, 10, 10, 8]
K_HDR = ["MARKET TITLE", "YES¢", "NO¢", "PICK", "VOLUME", "CLOSE ET", "PERIOD"]
sep_k = "-" * (sum(K_COL) + 2 * len(K_COL))

print(f"\n\n  KALSHI MARKETS  ({len(k_today)} markets today)")
print(row_fmt(K_HDR, K_COL))
print(sep_k)

if not k_today:
    print("  No Kalshi markets found.")
else:
    for m in sorted(k_today, key=lambda x: x["_close_et"] or datetime.max.replace(tzinfo=ET)):
        yes_p = m.get("yes_price") or 0
        no_p  = m.get("no_price")  or 0
        pick  = "YES" if yes_p >= no_p else "NO"
        print(row_fmt([
            (m.get("title") or "")[:38], yes_p, no_p, pick,
            f"${m.get('volume') or 0:,}", m["_close_str"], m["_period"],
        ], K_COL))

print(sep_k)
print("  PICK = higher-probability side  (YES if yes¢ >= no¢, else NO)")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 3 — SOFASCORE TODAY from Redis cache
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import redis as _r3, json as _j3
    _rc3 = _r3.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    _raw_s = _rc3.get("sofascore:today_events") or "[]"
    all_sf = _j3.loads(_raw_s)
    _live_st = {"live", "inprogress", "1h", "2h", "ht", "et", "pen", "progress"}
    live_sf = [e for e in all_sf if str(e.get("status","")).lower() in _live_st]
    show_sf = live_sf if live_sf else all_sf
except Exception:
    show_sf = []

S_COL = [32, 20, 12, 10]
S_HDR = ["MATCHUP", "SPORT", "STATUS", "TIME ET"]
sep_s = "-" * (sum(S_COL) + 2 * len(S_COL))

label = "LIVE NOW" if live_sf else "TODAY (no live yet)"
print(f"\n\n  SOFASCORE {label}  ({len(show_sf)} events)")
print(row_fmt(S_HDR, S_COL))
print(sep_s)

if not show_sf:
    print("  No Sofascore data in cache — bot loads this at 8 AM ET.")
else:
    for ev in show_sf[:60]:
        home    = ev.get("home_team") or ev.get("home") or "?"
        away    = ev.get("away_team") or ev.get("away") or "?"
        matchup = f"{away} @ {home}"[:32]
        sport   = (ev.get("sport") or ev.get("sport_key") or "")[:20]
        status  = str(ev.get("status") or "sched")[:12]
        ct_et   = parse_et(ev.get("commence_time") or ev.get("start_time") or "")
        t_str   = ct_et.strftime("%-I:%M %p") if ct_et else "—"
        print(row_fmt([matchup, sport, status, t_str], S_COL))

print(sep_s)
print(f"\n  {now.strftime('%I:%M %p ET')}  |  No API requests made — showing cached/DB data\n")
