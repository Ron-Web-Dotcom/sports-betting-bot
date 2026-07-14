"""
Run: python3 getlive_gamestoday.py

One-time full scan + display:
  1. Today's games (men + women, all sports) with odds & AI confidence — sorted by ET
  2. Kalshi markets — sorted by ET close time
  3. Sofascore live games right now
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from zoneinfo import ZoneInfo
from src.core.timezone import et_naive

ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
now = et_naive()

# ── FULL SCAN ─────────────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  LIVE GAMES TODAY  —  {now.strftime('%b %d, %Y  %I:%M %p ET')}")
print(f"{'='*80}\n")

print("  [1/4] Wiping stale cache...")
try:
    from src.workers.odds_worker import refresh_active_sports
    refresh_active_sports()
    print("        Done.\n")
except Exception as e:
    print(f"        Warning: {e}\n")

print("  [2/4] Sofascore — scanning all sports worldwide (men + women)...")
sf_result = {}
try:
    from src.workers.picks_worker import scan_todays_games
    sf_result = scan_todays_games() or {}
    print(f"        Done: {sf_result.get('day',0)} day  {sf_result.get('night',0)} night  ({sf_result.get('total',0)} total)\n")
except Exception as e:
    print(f"        Warning: {e}\n")

print("  [3/4] HardRock — pulling all odds...")
try:
    from src.workers.odds_worker import scan_and_save_odds
    odds_result = scan_and_save_odds() or {}
    print(f"        Done: {odds_result}\n")
except Exception as e:
    print(f"        Warning: {e}\n")

print("  [4/4] Kalshi — pulling all markets...")
try:
    from src.workers.odds_worker import scan_player_props
    kalshi_result = scan_player_props() or {}
    print(f"        Done: kalshi={kalshi_result.get('kalshi',0)} markets\n")
except Exception as e:
    print(f"        Warning: {e}\n")

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
    """Parse commence_time string → ET-aware datetime."""
    if not ct_str:
        return None
    try:
        dt = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 1 — TODAY'S GAMES (all sports, men + women) ordered by ET kickoff
# ═══════════════════════════════════════════════════════════════════════════════
from src.engines.odds_engine import get_latest_snapshots_by_game
snaps = get_latest_snapshots_by_game()

games = {}
for game_id, snap_list in snaps.items():
    if not snap_list:
        continue
    s0 = snap_list[0]
    ct_et = parse_et(s0.get("commence_time", ""))
    if not ct_et:
        continue
    if ct_et.date() != now.date():
        continue

    if game_id not in games:
        period = "NIGHT" if ct_et.hour >= 17 else "DAY"
        games[game_id] = {
            "away":    s0.get("away_team", "?"),
            "home":    s0.get("home_team", "?"),
            "sport":   s0.get("sport_key", ""),
            "time_et": ct_et.strftime("%-I:%M %p"),
            "period":  period,
            "sort_dt": ct_et,
            "h2h":     {},
        }

    for s in snap_list:
        if s.get("market") != "h2h":
            continue
        sel  = s.get("selection", "")
        odds = s.get("best_odds")
        if sel and odds is not None:
            cur = games[game_id]["h2h"].get(sel)
            if cur is None or odds > cur:
                games[game_id]["h2h"][sel] = odds

G_COL = [30, 9, 9, 12, 9, 10, 14]
G_HDR = ["MATCHUP", "TIME ET", "PERIOD", "BOT PICK", "ODDS", "IMPLIED", "CONFIDENCE"]
sep_g = "-" * (sum(G_COL) + 2 * len(G_COL))

print(f"\n{'━'*80}")
print(f"  TODAY'S GAMES  ({len(games)} games across all sports — men + women)")
print(f"{'━'*80}")
print(row_fmt(G_HDR, G_COL))
print(sep_g)

if not games:
    print("  No games found — Odds API may have no active events right now.")
else:
    for g in sorted(games.values(), key=lambda x: x["sort_dt"]):
        matchup        = f"{g['away']} @ {g['home']}"[:30]
        pick_sel, pick_odds = bot_pick(g["h2h"])
        prob           = implied_prob(pick_odds)
        sport_short    = (g["sport"]
                          .replace("basketball_", "").replace("soccer_", "")
                          .replace("americanfootball_", "").replace("icehockey_", "")
                          .replace("baseball_", ""))[:9]
        print(row_fmt([
            matchup,
            g["time_et"],
            g["period"],
            (pick_sel or "?")[:12],
            odds_fmt(pick_odds),
            f"{prob}%" if prob else "N/A",
            conf_label(prob),
        ], G_COL))

print(sep_g)
print("  ✅ = 80%+  ⚠️ = 70-79%  ❌ = <70%  |  Confidence = market implied prob of bot's pick")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 2 — KALSHI MARKETS today, ordered by ET close time
# ═══════════════════════════════════════════════════════════════════════════════
try:
    from src.apis.kalshi import get_sports_markets
    all_kalshi = get_sports_markets()
except Exception as e:
    all_kalshi = []
    print(f"\n  [Kalshi fetch error: {e}]")

k_today = []
for m in all_kalshi:
    ct_str = m.get("close_time") or m.get("expiration_time") or ""
    ct_et  = parse_et(ct_str)
    if ct_et and ct_et.date() == now.date():
        m["_close_et"] = ct_et
        m["_close_str"] = ct_et.strftime("%-I:%M %p")
        m["_period"] = "NIGHT" if ct_et.hour >= 17 else "DAY"
        k_today.append(m)
    elif not ct_str:
        m["_close_et"] = None
        m["_close_str"] = "—"
        m["_period"] = "—"
        k_today.append(m)

K_COL = [38, 6, 6, 6, 10, 9, 12]
K_HDR = ["MARKET TITLE", "YES¢", "NO¢", "PICK", "VOLUME", "CLOSE ET", "PERIOD"]
sep_k = "-" * (sum(K_COL) + 2 * len(K_COL))

print(f"\n\n{'━'*80}")
print(f"  KALSHI MARKETS  ({len(k_today)} markets today)")
print(f"{'━'*80}")
print(row_fmt(K_HDR, K_COL))
print(sep_k)

if not k_today:
    print("  No Kalshi markets found for today.")
else:
    for m in sorted(k_today, key=lambda x: x["_close_et"] or datetime.max.replace(tzinfo=ET)):
        yes_p = m.get("yes_price") or 0
        no_p  = m.get("no_price")  or 0
        pick  = "YES" if yes_p >= no_p else "NO"
        title = (m.get("title") or "")[:38]
        vol   = m.get("volume") or 0
        print(row_fmt([
            title, yes_p, no_p, pick, f"${vol:,}", m["_close_str"], m["_period"],
        ], K_COL))

print(sep_k)
print("  PICK = higher-probability side  (YES if yes¢ ≥ no¢, else NO)")

# ═══════════════════════════════════════════════════════════════════════════════
# TABLE 3 — SOFASCORE LIVE RIGHT NOW
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import redis as _redis, json as _json
    from src.core.config import REDIS_URL
    _r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    _raw = _r.get("sofascore:today_events") or "[]"
    _all = _json.loads(_raw)
    _live_statuses = {"live", "inprogress", "1h", "2h", "ht", "et", "pen", "progress"}
    live_events = [e for e in _all if str(e.get("status", "")).lower() in _live_statuses]
    # If nothing marked live yet, show all of today's games from Sofascore
    if not live_events:
        live_events = _all
except Exception:
    live_events = []

S_COL = [32, 24, 12, 10]
S_HDR = ["MATCHUP", "SPORT / LEAGUE", "STATUS", "TIME ET"]
sep_s = "-" * (sum(S_COL) + 2 * len(S_COL))

print(f"\n\n{'━'*80}")
print(f"  SOFASCORE LIVE NOW  ({len(live_events)} events)")
print(f"{'━'*80}")
print(row_fmt(S_HDR, S_COL))
print(sep_s)

if not live_events:
    print("  No live events found right now (or Sofascore not returning live data).")
else:
    for ev in live_events[:50]:
        home   = ev.get("home_team") or ev.get("home") or "?"
        away   = ev.get("away_team") or ev.get("away") or "?"
        matchup = f"{away} @ {home}"[:32]
        sport  = (ev.get("sport") or ev.get("sport_key") or "")[:24]
        status = str(ev.get("status") or "live")[:12]
        ct_et  = parse_et(ev.get("commence_time") or ev.get("start_time") or "")
        t_str  = ct_et.strftime("%-I:%M %p") if ct_et else "—"
        print(row_fmt([matchup, sport, status, t_str], S_COL))

print(sep_s)
print(f"\n  Scan complete — {now.strftime('%I:%M %p ET')}\n")
