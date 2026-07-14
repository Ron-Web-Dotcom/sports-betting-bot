"""
Run: python3 getlive_gamestoday.py          # read-only, instant
Run: python3 getlive_gamestoday.py --scan   # fresh scan first, then show table

Tables:
  1. SOFASCORE LIVE RIGHT NOW
  2. TODAY'S GAMES (men + women, all sports) — DAY before 4 PM ET, NIGHT 4 PM+
  3. PROPS (player, team, game — men + women)
  4. KALSHI MARKETS
"""
import os, sys, logging, json
sys.path.insert(0, os.path.dirname(__file__))
logging.disable(logging.CRITICAL)

_SCAN = "--scan" in sys.argv

from datetime import datetime
from zoneinfo import ZoneInfo
from src.core.timezone import et_naive

ET  = ZoneInfo("America/New_York")
now = et_naive()

# ── OPTIONAL FRESH SCAN ───────────────────────────────────────────────────────
if _SCAN:
    print(f"\n  [--scan] Running fresh scan for all sports (men + women)...")
    try:
        import src.apis.sofascore as _sf
        _sf._cb_failures = 0; _sf._cb_tripped_at = 0.0
        from src.workers.picks_worker import scan_todays_games
        r = scan_todays_games() or {}
        print(f"  Sofascore: {r.get('day',0)} day  {r.get('night',0)} night  ({r.get('total',0)} total)")
    except Exception as e:
        print(f"  Sofascore warning: {e}")
    try:
        from src.workers.odds_worker import scan_and_save_odds
        r2 = scan_and_save_odds() or {}
        print(f"  Odds API: {r2}")
    except Exception as e:
        print(f"  Odds warning: {e}")
    try:
        from src.workers.odds_worker import scan_player_props
        r3 = scan_player_props() or {}
        print(f"  Props + Kalshi: odds_api={r3.get('odds_api',0)}  kalshi={r3.get('kalshi',0)}")
    except Exception as e:
        print(f"  Props warning: {e}")
    print()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def implied_prob(odds):
    if odds is None: return None
    return round(abs(odds)/(abs(odds)+100)*100,1) if odds < 0 else round(100/(100+odds)*100,1)

def novig_prob(pick_odds, other_odds):
    """True win probability — no-vig + magnitude floor for huge favorites."""
    if pick_odds is None: return implied_prob(pick_odds)
    p1 = implied_prob(pick_odds) or 0
    p2 = implied_prob(other_odds) or 0
    total = p1 + p2
    nv = round(p1 / total * 100, 1) if total > 0 else round(p1, 1)

    # When the favorite's odds are massive, the market is near-certain — enforce a floor
    if pick_odds is not None and pick_odds < 0:
        o = abs(pick_odds)
        if   o >= 1000: nv = max(nv, 97.0)   # -1000 or worse → 97%+
        elif o >=  700: nv = max(nv, 95.0)   # -700  → 95%+
        elif o >=  500: nv = max(nv, 93.0)   # -500  → 93%+
        elif o >=  350: nv = max(nv, 90.0)   # -350  → 90%+
        elif o >=  250: nv = max(nv, 86.0)   # -250  → 86%+
        elif o >=  200: nv = max(nv, 83.0)   # -200  → 83%+

    return nv

def odds_fmt(odds):
    if odds is None: return "  N/A"
    return f"+{odds}" if odds > 0 else str(odds)

def conf_label(prob):
    if prob is None: return "N/A"
    if prob >= 80:   return f"{prob}% ✅"
    if prob >= 70:   return f"{prob}% ⚠️"
    return f"{prob}% ❌"

def row_fmt(cols, widths):
    return "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))

def parse_et(ct_str):
    if not ct_str: return None
    try:
        dt = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None

def short_sport(sk):
    return (sk.replace("basketball_","").replace("soccer_","")
              .replace("americanfootball_","").replace("icehockey_","")
              .replace("baseball_","").replace("tennis_","")
              .replace("mma_","").replace("boxing_",""))[:10]

# ── REDIS ─────────────────────────────────────────────────────────────────────
import redis as _redis
from src.core.config import REDIS_URL
_rc = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

print(f"\n{'='*90}")
print(f"  LIVE GAMES TODAY  —  {now.strftime('%b %d, %Y  %I:%M %p ET')}  |  DAY = before 4 PM ET  |  NIGHT = 4 PM+")
print(f"{'='*90}")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 1 — SOFASCORE LIVE RIGHT NOW (top of screen)
# ═══════════════════════════════════════════════════════════════════════════════
try:
    _all_sf = json.loads(_rc.get("sofascore:today_events") or "[]")
    _live_st = {"live","inprogress","1h","2h","ht","et","pen","progress","halftime","3rd quarter","4th quarter","1st quarter","2nd quarter"}
    live_sf  = [e for e in _all_sf if str(e.get("status","")).lower().replace(" ","") in {s.replace(" ","") for s in _live_st}
                or any(x in str(e.get("status","")).lower() for x in ["half","quarter","set","period","inning"])]
    all_sf_today = _all_sf
except Exception:
    live_sf = []; all_sf_today = []

def _sf_status_label(ev):
    """Return a clear status string: LIVE, SOON, or Scheduled."""
    raw = str(ev.get("status") or "").lower()
    _live_kw = {"live","inprogress","1h","2h","ht","et","pen","progress","halftime","inning","quarter","period","set"}
    if any(x in raw for x in _live_kw):
        return "🔴 LIVE"
    ct_et = parse_et(ev.get("commence_time") or ev.get("start_time") or "")
    if ct_et:
        _now_cmp = ct_et.replace(tzinfo=None) if ct_et.tzinfo else ct_et
        diff_min = (_now_cmp - now).total_seconds() / 60
        if -5 <= diff_min <= 90:
            return "⏰ SOON"
        if diff_min > 0:
            return "Scheduled"
    return raw[:12] or "—"

S_COL = [34, 18, 12, 10, 7]
S_HDR = ["MATCHUP", "SPORT", "STATUS", "OPENS ET", "PERIOD"]
sep_s = "-" * (sum(S_COL) + 2*len(S_COL))

print(f"\n  ── SOFASCORE LIVE RIGHT NOW ({len(live_sf)} live  /  {len(all_sf_today)} total today) ──")
print(row_fmt(S_HDR, S_COL))
print(sep_s)
show_sf = live_sf if live_sf else all_sf_today
if not show_sf:
    print("  No events found — bot loads Sofascore at 8 AM ET.")
else:
    for ev in sorted(show_sf, key=lambda e: (
        short_sport(e.get("sport") or e.get("sport_key") or ""),
        (parse_et(e.get("commence_time") or e.get("start_time") or "") or datetime.max.replace(tzinfo=ET)).replace(tzinfo=None)
    )):
        home    = ev.get("home_team") or "?"
        away    = ev.get("away_team") or "?"
        sport   = short_sport(ev.get("sport") or ev.get("sport_key") or "")
        slabel  = _sf_status_label(ev)
        ct_et   = parse_et(ev.get("commence_time") or ev.get("start_time") or "")
        opens   = ct_et.strftime("%-I:%M %p") if ct_et else "—"
        period  = "NIGHT" if ct_et and ct_et.hour >= 16 else ("DAY" if ct_et else "—")
        print(row_fmt([f"{away} @ {home}"[:34], sport[:18], slabel[:12], opens, period], S_COL))
print(sep_s)
print("  🔴 LIVE = in progress  |  ⏰ SOON = starting within 90 min  |  OPENS ET = Sofascore game start time")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 2 — TODAY'S GAMES + ODDS (both sides) from DB
# ═══════════════════════════════════════════════════════════════════════════════
from src.engines.odds_engine import get_latest_snapshots_by_game
snaps = get_latest_snapshots_by_game()

games = {}
for game_id, snap_list in snaps.items():
    if not snap_list: continue
    s0    = snap_list[0]
    ct_et = parse_et(s0.get("commence_time",""))
    if not ct_et or ct_et.replace(tzinfo=None).date() != now.date(): continue

    if game_id not in games:
        period = "NIGHT" if ct_et.hour >= 16 else "DAY"   # 4 PM cutoff
        games[game_id] = {
            "away":    s0.get("away_team","?"),
            "home":    s0.get("home_team","?"),
            "sport":   short_sport(s0.get("sport_key","")),
            "time_et": ct_et.strftime("%-I:%M %p"),
            "period":  period,
            "sort_dt": ct_et.replace(tzinfo=None) if ct_et else datetime.max,
            "h2h":     {},   # sel → best_odds
        }
    for s in snap_list:
        if s.get("market") != "h2h": continue
        sel, odds = s.get("selection",""), s.get("best_odds")
        if sel and odds is not None:
            cur = games[game_id]["h2h"].get(sel)
            if cur is None or odds > cur:
                games[game_id]["h2h"][sel] = odds

# Columns: MATCHUP | TIME | SPORT | PERIOD | AWAY ODDS | HOME ODDS | BOT PICK | CONF
G_COL = [30, 9, 10, 7, 10, 10, 16, 12]
G_HDR = ["MATCHUP", "TIME ET", "SPORT", "PERIOD", "AWAY ODDS", "HOME ODDS", "BOT PICK", "CONFIDENCE"]
sep_g = "-" * (sum(G_COL) + 2*len(G_COL))

day_g   = [g for g in games.values() if g["period"]=="DAY"]
night_g = [g for g in games.values() if g["period"]=="NIGHT"]

def _print_games(glist, label):
    print(f"\n  ── {label} ({len(glist)} games) ──")
    print(row_fmt(G_HDR, G_COL))
    print(sep_g)
    if not glist:
        print(f"  No {label.lower()} in DB.")
    else:
        for g in sorted(glist, key=lambda x: x["sort_dt"]):
            h2h      = g["h2h"]
            away_o   = h2h.get(g["away"]) or h2h.get(next((k for k in h2h if g["away"].split()[0].lower() in k.lower()), ""), None)
            home_o   = h2h.get(g["home"]) or h2h.get(next((k for k in h2h if g["home"].split()[0].lower() in k.lower()), ""), None)
            # best pick = most negative odds (favourite), else least positive
            if h2h:
                neg = {s:o for s,o in h2h.items() if o < 0}
                pick_sel  = min(neg, key=lambda s:neg[s]) if neg else min(h2h, key=lambda s:h2h[s])
                pick_odds = h2h[pick_sel]
                other_odds = next((o for s,o in h2h.items() if s != pick_sel), None)
            else:
                pick_sel, pick_odds, other_odds = "?", None, None
            prob = novig_prob(pick_odds, other_odds)
            print(row_fmt([
                f"{g['away']} @ {g['home']}"[:30],
                g["time_et"], g["sport"], g["period"],
                odds_fmt(away_o), odds_fmt(home_o),
                pick_sel[:16], conf_label(prob),
            ], G_COL))
    print(sep_g)

_print_games(day_g,   "DAY GAMES (before 4 PM ET)")
_print_games(night_g, "NIGHT GAMES (4 PM ET+)")
print("  ✅ 80%+  ⚠️ 70-79%  ❌ <70%  |  ODDS = best available across all books")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 3 — PROPS available today
# ═══════════════════════════════════════════════════════════════════════════════
try:
    raw_props = _rc.get("props:odds_api") or "[]"
    all_props = json.loads(raw_props)
except Exception:
    all_props = []

P_COL = [22, 20, 12, 8, 8, 8]
P_HDR = ["PLAYER / SUBJECT", "STAT", "SPORT", "LINE", "OVER", "UNDER"]
sep_p = "-" * (sum(P_COL) + 2*len(P_COL))

print(f"\n\n  ── PROPS AVAILABLE TODAY ({len(all_props)} props) ──")
print(row_fmt(P_HDR, P_COL))
print(sep_p)
if not all_props:
    print("  No props in cache — bot scans props every 20 min from 8 AM.")
else:
    seen_p = set()
    for p in all_props:
        subject = (p.get("player") or p.get("subject") or "")[:22]
        stat    = (p.get("stat") or p.get("market") or "")[:20]
        sport   = short_sport(p.get("sport_key",""))[:12]
        line    = str(p.get("line","—"))[:8]
        over_o  = odds_fmt(p.get("over_odds") or p.get("over"))[:8]
        under_o = odds_fmt(p.get("under_odds") or p.get("under"))[:8]
        key = f"{subject}|{stat}"
        if key in seen_p: continue
        seen_p.add(key)
        print(row_fmt([subject, stat, sport, line, over_o, under_o], P_COL))
print(sep_p)

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 4 — KALSHI MARKETS
# ═══════════════════════════════════════════════════════════════════════════════
try:
    raw_k    = _rc.get("kalshi:live_markets") or "[]"
    all_kalshi = json.loads(raw_k)
except Exception:
    all_kalshi = []

if not all_kalshi:
    try:
        from src.apis.kalshi import get_sports_markets
        all_kalshi = get_sports_markets()
    except Exception:
        all_kalshi = []

# Show ALL active markets — no strict date filter (Kalshi markets span multiple days)
k_rows = []
for m in all_kalshi:
    ct_et = parse_et(m.get("close_time") or m.get("expiration_time") or "")
    m["_close_str"] = ct_et.strftime("%-I:%M %p") if ct_et else "—"
    m["_period"]    = ("NIGHT" if ct_et and ct_et.hour >= 16 else "DAY") if ct_et else "—"
    m["_sort"]      = ct_et.replace(tzinfo=None) if ct_et else datetime.max
    k_rows.append(m)

K_COL = [40, 6, 6, 6, 10, 12, 8]
K_HDR = ["MARKET TITLE", "YES¢", "NO¢", "PICK", "VOLUME", "EVENT/CLOSE ET", "PERIOD"]
sep_k = "-" * (sum(K_COL) + 2*len(K_COL))

print(f"\n\n  ── KALSHI MARKETS ({len(k_rows)} active) ──")
print(row_fmt(K_HDR, K_COL))
print(sep_k)
if not k_rows:
    print("  No Kalshi markets in cache.")
else:
    for m in sorted(k_rows, key=lambda x: x["_sort"]):
        yes_raw = m.get("yes_price") or 0
        no_raw  = m.get("no_price")  or 0
        # Kalshi stores prices as 0–1 floats; display as cents (0–100)
        yes_p = round(yes_raw * 100) if yes_raw <= 1 else round(yes_raw)
        no_p  = round(no_raw  * 100) if no_raw  <= 1 else round(no_raw)
        pick  = "YES" if yes_p >= no_p else "NO"
        print(row_fmt([
            (m.get("title") or "")[:40], f"{yes_p}¢", f"{no_p}¢", pick,
            f"${m.get('volume') or 0:,}", m["_close_str"], m["_period"],
        ], K_COL))
print(sep_k)
print("  PICK = higher-probability side  (YES if yes¢ >= no¢, else NO)")
print("  EVENT/CLOSE ET = when the game starts / market settles (same time for most props)")

# ── BOT'S KALSHI SLIP (single best pick) ─────────────────────────────────────
print(f"\n{'='*90}")
print(f"  BOT'S KALSHI SLIP")
print(f"{'='*90}")
if k_rows:
    _scored = []
    for m in k_rows:
        yes_raw = m.get("yes_price") or 0
        no_raw  = m.get("no_price")  or 0
        yc = round(yes_raw * 100) if yes_raw <= 1 else round(yes_raw)
        nc = round(no_raw  * 100) if no_raw  <= 1 else round(no_raw)
        vol = m.get("volume") or 0
        if vol < 100:        # skip tiny/illiquid markets
            continue
        winner_c = max(yc, nc)
        loser_c  = min(yc, nc)
        spread   = winner_c - loser_c   # bigger = more lopsided = more confident
        side     = "YES" if yc >= nc else "NO"
        _scored.append((spread, winner_c, vol, m, side, yc, nc))

    if _scored:
        _scored.sort(key=lambda x: (x[0], x[2]), reverse=True)   # sort by spread, then volume
        spread, winner_c, vol, best, side, yc, nc = _scored[0]
        ct_et = parse_et(best.get("close_time") or best.get("expiration_time") or "")
        opens = ct_et.strftime("%-I:%M %p ET") if ct_et else "—"
        other_c = nc if side == "YES" else yc
        if winner_c >= 85:   conf_label_k = "HIGH ✅"
        elif winner_c >= 70: conf_label_k = "MEDIUM ⚠️"
        else:                conf_label_k = "LOW ❌"
        print(f"\n  MARKET : {best.get('title','')}")
        print(f"  PICK   : {side}  ({winner_c}¢ vs {other_c}¢  |  {spread}¢ edge)")
        print(f"  CONF   : {conf_label_k}  ({winner_c}¢ is a {'high' if winner_c >= 85 else 'medium' if winner_c >= 70 else 'low'} price — market strongly favors {side})")
        print(f"  OPENS  : {opens}")
        print(f"  VOLUME : ${vol:,}")
    else:
        print("\n  No liquid Kalshi markets found (volume < $100).")
else:
    print("\n  No Kalshi markets in cache.")

print(f"\n  {now.strftime('%I:%M %p ET')}  |  Read from DB/cache only — zero API requests\n")
