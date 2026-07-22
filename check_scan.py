#!/usr/bin/env python3
"""
check_scan.py — quick snapshot of today's data from Redis cache.

Usage:
    python3 check_scan.py              # read from cache (instant)
    python3 check_scan.py --scan       # re-run all scans first, then show
    python3 check_scan.py --sofascore  # trigger Sofascore scan only
    python3 check_scan.py --odds       # trigger Odds API scan only
"""
import os, sys, json, logging
sys.path.insert(0, os.path.dirname(__file__))
logging.disable(logging.CRITICAL)

# Load .env so API keys and REDIS_URL are available when running manually
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=False)
    except ImportError:
        # fallback: parse manually
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip())

from datetime import datetime
from zoneinfo import ZoneInfo

ET  = ZoneInfo("America/New_York")
now = datetime.now(ET)

_SCAN       = "--scan"       in sys.argv
_SF_ONLY    = "--sofascore"  in sys.argv
_ODDS_ONLY  = "--odds"       in sys.argv

# ── REDIS ────────────────────────────────────────────────────────────────────
import redis as _redis
from src.core.config import REDIS_URL
_rc = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

# ── OPTIONAL SCANS ────────────────────────────────────────────────────────────
if _SCAN or _SF_ONLY:
    print("  [scan] Fetching Sofascore today events...")
    try:
        import src.apis.sofascore as _sf
        _sf._cb_failures = 0; _sf._cb_tripped_at = 0.0
        from src.workers.picks_worker import scan_todays_games
        r = scan_todays_games() or {}
        print(f"  Sofascore: {r.get('total', 0)} total  ({r.get('day',0)} day / {r.get('night',0)} night)")
    except Exception as e:
        print(f"  Sofascore error: {e}")

if _SCAN or _ODDS_ONLY:
    # Ensure Sofascore is populated first so the sport-key filter works
    _sf_cached = _rc.get("sofascore:today_events")
    if not _sf_cached or _sf_cached == "[]":
        print("  [scan] Sofascore cache empty — fetching first so Odds API filter works...")
        try:
            import src.apis.sofascore as _sf_mod
            _sf_mod._cb_failures = 0; _sf_mod._cb_tripped_at = 0.0
            from src.workers.picks_worker import scan_todays_games
            _sfr = scan_todays_games() or {}
            print(f"  Sofascore: {_sfr.get('total',0)} events loaded")
        except Exception as _sfe:
            print(f"  Sofascore warning: {_sfe}")
    print("  [scan] Fetching Odds API...")
    try:
        from src.workers.odds_worker import scan_odds_only
        r2 = scan_odds_only() or {}
        total = r2.pop("_total_events", 0)
        elapsed = r2.pop("_elapsed_s", 0)
        print(f"  Odds API: {total} events across {len(r2)} sports  ({elapsed}s)")
    except Exception as e:
        print(f"  Odds API error: {e}")

if _SCAN or _ODDS_ONLY:
    print("  [scan] Fetching props + Kalshi...")
    try:
        from src.workers.odds_worker import scan_player_props
        r3 = scan_player_props() or {}
        print(f"  Props/Kalshi: odds_props={r3.get('odds_props',{}).get('props',0)}  kalshi_submarkets={r3.get('kalshi_submarkets',{}).get('markets',0)}")
    except Exception as e:
        print(f"  Props/Kalshi error: {e}")

if _SCAN or _SF_ONLY or _ODDS_ONLY:
    print()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _parse_et(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(ET)
    except Exception:
        return None

def _time_et(s: str) -> str:
    dt = _parse_et(s)
    return dt.strftime("%-I:%M %p") if dt else "—"

def _h2h_from_markets(markets: dict, away_team: str, home_team: str) -> tuple:
    """Return (away_odds, home_odds) as best American odds from the h2h market.
    markets structure: {"h2h": {"Team A": [{"american_odds": -110, ...}], ...}}
    """
    h2h = markets.get("h2h", {})
    if not h2h or not isinstance(h2h, dict):
        return None, None
    # Each key is a team name, value is list of book entries sorted best-first
    away_n = away_team.lower()
    home_n = home_team.lower()
    away_o = home_o = None
    for team_name, entries in h2h.items():
        if entries is None:
            continue
        # Structure 1: {"home": -150, "away": +130}  (plain int from scan_player_props)
        if isinstance(entries, int):
            best = entries
        # Structure 2: {"TeamName": [{"american_odds": -110, ...}]}  (list from normalise_event)
        elif isinstance(entries, list) and entries:
            first = entries[0]
            best = first.get("american_odds") if isinstance(first, dict) else (int(first) if first else None)
        else:
            continue
        tn = team_name.lower()
        # "home"/"away" keys from scan_player_props structure
        if tn == "away":
            away_o = best
        elif tn == "home":
            home_o = best
        # team-name keys from normalise_event structure
        elif away_n in tn or tn in away_n:
            away_o = best
        elif home_n in tn or tn in home_n:
            home_o = best
    return away_o, home_o

def _odds_str(o) -> str:
    if o is None:
        return "  N/A"
    return f"+{o}" if o > 0 else str(o)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — SOFASCORE TODAY
# ─────────────────────────────────────────────────────────────────────────────
_sf_raw  = _rc.get("sofascore:today_events")
_sf_evs  = json.loads(_sf_raw) if _sf_raw else []
_sf_live = [e for e in _sf_evs if any(
    x in str(e.get("status_type","") or e.get("status","")).lower()
    for x in ["inprogress","live","halftime","1h","2h","ht"]
)]

print(f"\n{'='*80}")
print(f"  CHECK SCAN  —  {now.strftime('%b %d, %Y  %I:%M %p ET')}")
print(f"{'='*80}")

print(f"\n=== SOFASCORE TODAY ({len(_sf_evs)} games  /  {len(_sf_live)} live) ===")
if not _sf_evs:
    print("  (empty — bot runs Sofascore scan at 8 AM ET;  use --sofascore to refresh now)")
else:
    # group by sport
    _sf_by_sport: dict[str, list] = {}
    for ev in _sf_evs:
        sk = ev.get("sport") or ev.get("sport_key") or "unknown"
        _sf_by_sport.setdefault(sk, []).append(ev)
    for sk, evs in sorted(_sf_by_sport.items()):
        print(f"\n  [{sk}]")
        for ev in sorted(evs, key=lambda e: _parse_et(e.get("commence_time","")) or datetime.max.replace(tzinfo=ET)):
            t    = _time_et(ev.get("commence_time",""))
            home = ev.get("home_team","?")
            away = ev.get("away_team","?")
            st   = ev.get("status_type","") or ev.get("status","")
            live_tag = " 🔴" if "inprogress" in st.lower() or "live" in st.lower() else ""
            print(f"    {t:>8}  {away:<28} @ {home}{live_tag}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — ODDS API TODAY (from odds:events_grouped cache)
# ─────────────────────────────────────────────────────────────────────────────
_odds_raw     = _rc.get("odds:events_grouped")
_odds_grouped = json.loads(_odds_raw) if _odds_raw else {}

_total_odds_events = sum(len(v) for v in _odds_grouped.values())
print(f"\n\n=== ODDS API TODAY ({_total_odds_events} events  /  {len(_odds_grouped)} sports) ===")
if not _odds_grouped:
    print("  (empty — use --odds to refresh)")
else:
    for sk in sorted(_odds_grouped.keys()):
        evs = _odds_grouped[sk]
        if not evs:
            continue
        print(f"\n  [{sk}]")
        for ev in sorted(evs, key=lambda e: _parse_et(e.get("commence_time","")) or datetime.max.replace(tzinfo=ET)):
            t    = _time_et(ev.get("commence_time",""))
            home = ev.get("home_team","?")
            away = ev.get("away_team","?")
            markets = ev.get("markets", {})
            away_o, home_o = _h2h_from_markets(markets, away, home)
            odds_part = f"  [{_odds_str(away_o)} / {_odds_str(home_o)}]" if (away_o or home_o) else "  [no h2h odds]"
            print(f"    {t:>8}  {away:<28} @ {home:<28}{odds_part}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — OVERLAP
# ─────────────────────────────────────────────────────────────────────────────
_sf_sports   = {ev.get("sport") or ev.get("sport_key","") for ev in _sf_evs}
_odds_sports = set(_odds_grouped.keys())
print(f"\n\n=== SPORT KEY OVERLAP ===")
print(f"  Sofascore only : {_sf_sports  - _odds_sports or 'set()'}")
print(f"  Odds API only  : {_odds_sports - _sf_sports  or 'set()'}")
print(f"  In both        : {_sf_sports  & _odds_sports or 'set()'}")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — PROPS SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
_props_raw = _rc.get("props:odds_api")
_props     = json.loads(_props_raw) if _props_raw else []
print(f"\n\n=== PROPS ({len(_props)} total) ===")
if not _props:
    print("  (empty — run: python3 check_scan.py --odds)")
else:
    _p_by_sport: dict[str, list] = {}
    for p in _props:
        sk = p.get("sport_key","?")
        _p_by_sport.setdefault(sk, []).append(p)

    for sk in sorted(_p_by_sport.keys()):
        ps = _p_by_sport[sk]
        # group by game (event_id or home+away)
        _by_game: dict[str, list] = {}
        for p in ps:
            gkey = p.get("event_id") or f"{p.get('away_team','')}@{p.get('home_team','')}"
            _by_game.setdefault(gkey, []).append(p)

        # Count unique player+stat combos for cleaner summary
        _uniq = len({(p.get("player") or p.get("subject",""), p.get("stat","")) for p in ps})
        print(f"\n  [{sk}]  {_uniq} unique player props across {len(_by_game)} games")

        W = [26, 24, 6, 8, 8]
        hdr = f"    {'PLAYER/SUBJECT':<{W[0]}}  {'STAT':<{W[1]}}  {'LINE':>{W[2]}}  {'OVER':>{W[3]}}  {'UNDER':>{W[4]}}"
        sep = "    " + "-" * (sum(W) + 2 * len(W))

        def _fmt_side(v):
            if v is None: return "  N/A"
            if isinstance(v, dict):
                best = max(v.values()) if v else None
                return _odds_str(best) if best is not None else "  N/A"
            return _odds_str(int(v))

        for gkey, gprops in _by_game.items():
            sample = gprops[0]
            away_g = sample.get("away_team","")
            home_g = sample.get("home_team","")
            t_g    = _time_et(sample.get("commence_time",""))
            if away_g and home_g:
                print(f"\n    ── {t_g}  {away_g} @ {home_g}")
            print(hdr)
            print(sep)

            # Group by player+stat, collect all lines — one block per player
            _grouped_ps: dict[tuple, list] = {}
            for p in gprops:
                pkey = (p.get("player") or p.get("subject",""), p.get("stat",""))
                _grouped_ps.setdefault(pkey, []).append(p)

            last_stat = None
            for (player, stat), lines in sorted(_grouped_ps.items()):
                # Print blank line between stat groups for readability
                if stat != last_stat:
                    if last_stat is not None:
                        print()
                    last_stat = stat
                subj = player[:W[0]]
                stat_s = stat[:W[1]]
                # Sort lines numerically; print first line inline, rest indented
                lines_sorted = sorted(lines, key=lambda x: float(x.get("line") or 0))
                for i, p in enumerate(lines_sorted):
                    line_v  = str(p.get("line") or "—")
                    over_o  = _fmt_side(p.get("over_odds") or p.get("over"))
                    under_o = _fmt_side(p.get("under_odds") or p.get("under"))
                    if i == 0:
                        print(f"    {subj:<{W[0]}}  {stat_s:<{W[1]}}  {line_v:>{W[2]}}  {over_o:>{W[3]}}  {under_o:>{W[4]}}")
                    else:
                        # continuation line — blank name/stat, just show extra line
                        print(f"    {'':>{W[0]}}  {'':>{W[1]}}  {line_v:>{W[2]}}  {over_o:>{W[3]}}  {under_o:>{W[4]}}")

print(f"\n{'='*80}\n")
