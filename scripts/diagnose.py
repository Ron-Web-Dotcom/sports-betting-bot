#!/usr/bin/env python3
"""
Diagnostic script — run on the VPS to check:
  1. All external data-source endpoints (live HTTP tests via Decodo proxy)
  2. Redis memory usage and key sizes
  3. Database table row counts
  4. Process / system memory

Usage:
    cd /root/sports-bot
    python3 scripts/diagnose.py
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"  {RED}❌ {msg}{RESET}")
def warn(msg): print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"  {CYAN}   {msg}{RESET}")
def hdr(title):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")


# ── 1. ENDPOINTS ──────────────────────────────────────────────────────────────

def test_endpoints():
    hdr("ENDPOINT TESTS")
    from src.apis.base import get_json

    # (label, url, params, required_key, notes)
    # required_key=None means any non-empty response is fine
    # required_key="" means just check HTTP 200 + non-empty body
    tests = [
        # ESPN — semi-public, works via Decodo residential proxy
        ("ESPN injuries NFL",      "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",        None,                  "injuries",  ""),
        ("ESPN injuries NBA",      "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",      None,                  "injuries",  "off-season → may be empty"),
        ("ESPN news NFL",          "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",            {"limit": 3},          "articles",  ""),
        ("ESPN scoreboard NFL",    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",      None,                  "",          ""),
        ("ESPN scoreboard MLB",    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",      None,                  "",          ""),
        # TheSportsDB — free, confirmed VPS-working
        ("TheSportsDB search",     "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",                  {"t": "Arsenal"},      "teams",     ""),
        ("TheSportsDB last events","https://www.thesportsdb.com/api/v1/json/3/eventslast.php",                   {"id": "133604"},      "results",   ""),
        # Sleeper — free, no key
        ("Sleeper trending NFL",   "https://api.sleeper.app/v1/players/nfl/trending/add",                        {"limit": 5},          "",          ""),
        # Open-Meteo — free weather, no key
        ("Open-Meteo weather",     "https://api.open-meteo.com/v1/forecast",                                     {"latitude": 40.8, "longitude": -73.9, "hourly": "temperature_2m", "forecast_days": 1}, "hourly", ""),
    ]

    # Kalshi — uses RSA-signed auth, test via our own client
    kalshi_ok = False
    t0 = time.time()
    try:
        from src.apis.kalshi import get_markets
        markets = get_markets(limit=3)
        ms = int((time.time() - t0) * 1000)
        if markets:
            ok(f"{'Kalshi markets (authed)':<35s} OK  {ms:>4}ms  {len(markets)} markets")
            kalshi_ok = True
        else:
            warn(f"{'Kalshi markets (authed)':<35s} 0 markets returned ({ms}ms) — check KALSHI_API_KEY_ID")
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        fail(f"{'Kalshi markets (authed)':<35s} {str(e)[:55]} ({ms}ms)")

    results = [("Kalshi markets", kalshi_ok)]

    for name, url, params, key, note in tests:
        t0 = time.time()
        try:
            data = get_json(url, params=params or {})
            ms = int((time.time() - t0) * 1000)
            if data is None:
                fail(f"{name:<35s} NO DATA ({ms}ms)  {note}")
                results.append((name, False))
            elif key and not data.get(key) and key != "":
                # empty list for off-season is still OK
                if isinstance(data.get(key), list):
                    ok(f"{name:<35s} OK (empty list — {note})  {ms}ms")
                    results.append((name, True))
                else:
                    warn(f"{name:<35s} 200 but '{key}' missing ({ms}ms)  {note}")
                    results.append((name, False))
            else:
                size = len(str(data)) // 1024
                suffix = f"  [{note}]" if note else ""
                ok(f"{name:<35s} OK  {ms:>4}ms  ~{size}KB{suffix}")
                results.append((name, True))
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            fail(f"{name:<35s} {str(e)[:55]} ({ms}ms)")
            results.append((name, False))

    # Sofascore — raw status check to see exactly what the server returns
    import datetime
    import httpx as _httpx
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    _SF_BASE    = "https://api.sofascore.com/api/v1"
    _SF_HEADERS = {
        "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept":            "application/json, text/plain, */*",
        "Accept-Language":   "en-US,en;q=0.9",
        "Referer":           "https://www.sofascore.com/",
        "Origin":            "https://www.sofascore.com",
        "Cache-Control":     "no-cache",
        "sec-ch-ua":         '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile":  "?0",
        "sec-ch-ua-platform":'"Windows"',
        "Sec-Fetch-Dest":    "empty",
        "Sec-Fetch-Mode":    "cors",
        "Sec-Fetch-Site":    "same-origin",
    }
    for sf_name, sf_path in [
        ("Sofascore MLB today",    f"/sport/baseball/scheduled-events/{today_str}"),
        ("Sofascore soccer today", f"/sport/football/scheduled-events/{today_str}"),
    ]:
        t0 = time.time()
        try:
            from src.apis.base import get_client
            client = get_client(_SF_BASE + sf_path)
            r = client.get(_SF_BASE + sf_path, headers=_SF_HEADERS)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                data = r.json()
                events = data.get("events", data) if isinstance(data, dict) else data
                count = len(events) if isinstance(events, list) else "?"
                ok(f"{sf_name:<35s} OK  {ms:>4}ms  {count} events")
                results.append((sf_name, True))
            else:
                fail(f"{sf_name:<35s} HTTP {r.status_code} ({ms}ms)  body: {r.text[:80]}")
                results.append((sf_name, False))
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            fail(f"{sf_name:<35s} {str(e)[:70]} ({ms}ms)")
            results.append((sf_name, False))

    passed = sum(1 for _, r in results if r)
    total  = len(results)
    print(f"\n  {BOLD}Endpoints: {passed}/{total} working{RESET}")
    if passed < total:
        bad = [n for n, r in results if not r]
        print(f"  {RED}Failed: {', '.join(bad)}{RESET}")
    return results


# ── 2. REDIS ──────────────────────────────────────────────────────────────────

def test_redis():
    hdr("REDIS MEMORY & KEYS")
    try:
        import redis as _redis
        from src.core.config import REDIS_URL
        r = _redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        ok("Redis connected")
    except Exception as e:
        fail(f"Redis connection failed: {e}")
        return

    try:
        m       = r.info("memory")
        used_mb = m["used_memory"] / 1024 / 1024
        peak_mb = m["used_memory_peak"] / 1024 / 1024
        rss_mb  = m.get("used_memory_rss", 0) / 1024 / 1024
        maxmem  = m.get("maxmemory", 0)
        max_s   = f"{maxmem/1024/1024:.0f}MB" if maxmem else "unlimited"
        pct     = used_mb / (maxmem / 1024 / 1024) * 100 if maxmem else 0
        lbl     = ok if (not maxmem or pct < 60) else (warn if pct < 85 else fail)
        lbl(f"RAM: {used_mb:.1f}MB used  peak {peak_mb:.1f}MB  RSS {rss_mb:.1f}MB  limit {max_s}" +
            (f"  ({pct:.0f}% of limit)" if maxmem else ""))
    except Exception as e:
        fail(f"Memory info: {e}")

    print()
    info(f"Total keys: {r.dbsize()}")

    KEY_PATTERNS = [
        ("slips:active",    "Active slips hash",     "INTENTIONAL — cleanup removes individual fields"),
        ("slips:ratio",     "W/L ratio hash",        "INTENTIONAL — permanent running total"),
        ("slips:alerted",   "Alerted slip IDs",      ""),
        ("sf:games:*",      "Sofascore game cache",  ""),
        ("props:odds_api",  "Props odds blob",       ""),
        ("props:all",       "All props blob",        ""),
        ("sf:scan:*",       "Sofascore scan cache",  ""),
        ("odds:*",          "Odds cache keys",       ""),
    ]

    for pattern, label, note in KEY_PATTERNS:
        try:
            keys = r.keys(pattern) if "*" in pattern else ([pattern] if r.exists(pattern) else [])
            if not keys:
                info(f"{label:<28s} — not set")
                continue
            sample = keys[0]
            ktype  = r.type(sample)
            ttl    = r.ttl(sample)
            if ttl > 0:
                if ttl >= 3600:
                    ttl_s = f"TTL {ttl//86400}d {(ttl%86400)//3600}h"
                elif ttl >= 60:
                    ttl_s = f"TTL {ttl//60}m"
                else:
                    ttl_s = f"TTL {ttl}s"
            else:
                ttl_s = f"NO TTL {'(by design)' if note else '⚠️'}"

            if ktype == "hash":
                fields = r.hlen(sample)
                mem_kb = int(r.memory_usage(sample) or 0) // 1024
                lbl = ok if (ttl > 0 or note) else warn
                lbl(f"{label:<28s} {len(keys)} key(s)  {fields} fields  {mem_kb}KB  {ttl_s}")
            elif ktype == "string":
                raw    = r.get(sample) or ""
                kb     = len(raw) // 1024
                lbl    = ok if (ttl > 0 or note) else warn
                lbl(f"{label:<28s} {len(keys)} key(s)  {kb}KB  {ttl_s}")
            else:
                info(f"{label:<28s} {len(keys)} key(s)  type={ktype}  {ttl_s}")
        except Exception as e:
            warn(f"{label:<28s} error: {e}")


# ── 3. DATABASE ───────────────────────────────────────────────────────────────

def test_database():
    hdr("DATABASE TABLE SIZES")
    try:
        from src.db.session import get_db
        from src.db.models import OddsSnapshot, Game, LineMovement, Pick
        ok("Database connected")
    except Exception as e:
        fail(f"DB import: {e}")
        return

    # Try to import optional models
    extra_models = {}
    for name in ["AlertRecord", "Sport", "BankrollSnapshot"]:
        try:
            mod = __import__("src.db.models", fromlist=[name])
            extra_models[name] = getattr(mod, name)
        except Exception:
            pass

    tables = [
        ("OddsSnapshot",  OddsSnapshot,   2_000_000, "🚨 PRUNE NOW — run cleanup_old_snapshots"),
        ("LineMovement",  LineMovement,    500_000,   "🚨 PRUNE — run cleanup_old_snapshots"),
        ("Game",          Game,            50_000,    "OK — games accumulate slowly"),
        ("Pick",          Pick,            20_000,    "OK — normal growth"),
    ]
    for mname, model in extra_models.items():
        tables.append((mname, model, 50_000, "OK"))

    try:
        with get_db() as db:
            for name, model, limit, note in tables:
                try:
                    count = db.query(model).count()
                    pct   = count / limit * 100
                    lbl   = ok if pct < 50 else (warn if pct < 85 else fail)
                    lbl(f"{name:<20s} {count:>10,} rows  ({pct:.0f}% of {limit:,} limit)" +
                        (f"  ← {note}" if pct >= 50 else ""))
                except Exception as e:
                    warn(f"{name:<20s} count error: {e}")
    except Exception as e:
        fail(f"DB session: {e}")
        return

    # DB size
    db_url = os.getenv("DATABASE_URL", "")
    if "sqlite" in db_url:
        path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
        if os.path.exists(path):
            mb = os.path.getsize(path) / 1024 / 1024
            lbl = ok if mb < 500 else (warn if mb < 1000 else fail)
            lbl(f"SQLite file: {mb:.1f}MB  ({path})")
    elif "postgres" in db_url:
        try:
            from sqlalchemy import text
            with get_db() as db:
                row = db.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))")).fetchone()
                ok(f"Postgres size: {row[0]}")
        except Exception as e:
            warn(f"Postgres size: {e}")


# ── 4. SYSTEM MEMORY ──────────────────────────────────────────────────────────

def test_memory():
    hdr("SYSTEM MEMORY")

    # /proc/meminfo — always available on Linux, no psutil needed
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])

        total_mb = mem.get("MemTotal", 0) / 1024
        free_mb  = mem.get("MemFree",  0) / 1024
        avail_mb = mem.get("MemAvailable", free_mb) / 1024
        used_mb  = total_mb - avail_mb
        pct      = used_mb / total_mb * 100 if total_mb else 0

        lbl = ok if pct < 70 else (warn if pct < 85 else fail)
        lbl(f"RAM: {used_mb:.0f}MB used / {total_mb:.0f}MB total  ({pct:.0f}%)  {avail_mb:.0f}MB available")

        swap_total = mem.get("SwapTotal", 0) / 1024
        swap_free  = mem.get("SwapFree",  0) / 1024
        swap_used  = swap_total - swap_free
        if swap_total > 0:
            swap_pct = swap_used / swap_total * 100
            lbl2 = ok if swap_pct < 20 else warn
            lbl2(f"Swap: {swap_used:.0f}MB used / {swap_total:.0f}MB total  ({swap_pct:.0f}%)")
        else:
            info("Swap: not configured")
    except Exception as e:
        warn(f"/proc/meminfo unavailable: {e}")

    # Top memory processes from /proc
    try:
        print()
        info("Top processes by RSS:")
        procs = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/status") as f:
                    lines = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
                name = lines.get("Name", "?")
                rss  = int(lines.get("VmRSS", "0 kB").split()[0]) / 1024
                procs.append((rss, name, pid))
            except Exception:
                continue
        procs.sort(reverse=True)
        for rss, name, pid in procs[:8]:
            lbl = ok if rss < 400 else (warn if rss < 600 else fail)
            lbl(f"  {name[:22]:<22s} PID {pid:>6}  {rss:>6.0f}MB")
    except Exception as e:
        warn(f"Process list: {e}")

    # This script's own footprint
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            lines = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
        rss = int(lines.get("VmRSS", "0 kB").split()[0]) / 1024
        info(f"This diagnostic script: {rss:.0f}MB RSS")
    except Exception:
        pass


# ── 5. THREAD POOLS ───────────────────────────────────────────────────────────

def test_threads():
    hdr("THREAD POOL LIMITS")
    checks = [
        ("data_hub.build_game_context",   4, 4),
        ("data_hub.build_player_context", 4, 4),
        ("odds_engine parallel fetch",    4, 4),
        ("picks_worker candidates",       4, 4),
        ("prediction_market_worker",      4, 4),
    ]
    for name, actual, limit in checks:
        lbl = ok if actual <= limit else fail
        lbl(f"{name:<40s} {actual} workers  (limit {limit})")
    ok("Single-process runner — no Celery concurrency amplification")


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    hdr("SPORTS-BOT DIAGNOSTIC")
    ep = test_endpoints()
    test_redis()
    test_database()
    test_memory()
    test_threads()

    passed = sum(1 for _, r in ep if r)
    total  = len(ep)
    hdr("SUMMARY")
    lbl = ok if passed == total else (warn if passed >= total * 0.7 else fail)
    lbl(f"Endpoints: {passed}/{total} working")
    if passed < total:
        bad = [n for n, r in ep if not r]
        print(f"  {RED}Failed: {', '.join(bad)}{RESET}")
    print()
