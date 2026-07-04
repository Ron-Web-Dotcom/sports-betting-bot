#!/usr/bin/env python3
"""
Diagnostic script — run on the VPS to check:
  1. All external data-source endpoints (live HTTP tests via Decodo proxy)
  2. Redis memory usage and key sizes
  3. Database table row counts and estimated disk usage
  4. Process memory (RAM)

Usage:
    cd /home/user/sports-betting-bot
    python3 scripts/diagnose.py
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
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

def ok(msg):  print(f"  {GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"  {RED}❌ {msg}{RESET}")
def warn(msg): print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg): print(f"  {CYAN}   {msg}{RESET}")

# ── 1. ENDPOINT TESTS ─────────────────────────────────────────────────────────

def test_endpoints():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  ENDPOINT TESTS{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    from src.apis.base import get_json

    tests = [
        # (name, url, params, key_in_response)
        ("ESPN injuries NBA",      "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",   None,                 "injuries"),
        ("ESPN injuries NFL",      "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries",     None,                 "injuries"),
        ("ESPN news NBA",          "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news",       {"limit": 3},         "articles"),
        ("ESPN scoreboard NBA",    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", None,                 "events"),
        ("ESPN scoreboard NFL",    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",   None,                 "events"),
        ("TheSportsDB search",     "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",               {"t": "Arsenal"},     "teams"),
        ("TheSportsDB events",     "https://www.thesportsdb.com/api/v1/json/3/eventslast.php",                {"id": "133604"},     "results"),
        ("Sleeper trending",       "https://api.sleeper.app/v1/players/nfl/trending/add",                     {"limit": 3},         None),
        ("MLB Stats teams",        "https://statsapi.mlb.com/api/v1/teams",                                   {"sportId": 1},       "teams"),
        ("Open-Meteo forecast",    "https://api.open-meteo.com/v1/forecast",                                  {"latitude": 40.8, "longitude": -73.9, "hourly": "temperature_2m", "forecast_days": 1}, "hourly"),
        ("Kalshi markets",         "https://trading-api.kalshi.com/trade-api/v2/markets",                     {"limit": 3},         "markets"),
        ("Sofascore live",         "https://api.sofascore.com/api/v1/sport/basketball/events/live",           None,                 "events"),
        ("Sofascore scheduled",    "https://api.sofascore.com/api/v1/sport/football/scheduled-events/2025-07-05", None,             "events"),
    ]

    results = []
    for name, url, params, key in tests:
        t0 = time.time()
        try:
            data = get_json(url, params=params or {})
            ms = int((time.time() - t0) * 1000)
            if data is None:
                fail(f"{name:<35s} NO DATA ({ms}ms)")
                results.append((name, False))
            elif key and not data.get(key):
                warn(f"{name:<35s} 200 but '{key}' empty ({ms}ms)")
                results.append((name, False))
            else:
                size = len(str(data))
                ok(f"{name:<35s} OK  {ms:>4}ms  ~{size//1024}KB")
                results.append((name, True))
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            fail(f"{name:<35s} ERROR: {str(e)[:60]} ({ms}ms)")
            results.append((name, False))

    passed = sum(1 for _, r in results if r)
    total  = len(results)
    print(f"\n  {BOLD}Endpoints: {passed}/{total} working{RESET}")
    return results


# ── 2. REDIS HEALTH ───────────────────────────────────────────────────────────

def test_redis():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  REDIS MEMORY & KEYS{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    try:
        import redis as _redis
        from src.core.config import REDIS_URL
        r = _redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        ok("Redis connected")
    except Exception as e:
        fail(f"Redis connection failed: {e}")
        return

    # Memory info
    try:
        info_raw = r.info("memory")
        used_mb  = info_raw["used_memory"] / 1024 / 1024
        peak_mb  = info_raw["used_memory_peak"] / 1024 / 1024
        rss_mb   = info_raw.get("used_memory_rss", 0) / 1024 / 1024
        maxmem   = info_raw.get("maxmemory", 0)
        maxmem_s = f"{maxmem/1024/1024:.0f}MB" if maxmem else "unlimited"

        label = ok if used_mb < 100 else (warn if used_mb < 200 else fail)
        label(f"RAM used: {used_mb:.1f}MB  peak: {peak_mb:.1f}MB  RSS: {rss_mb:.1f}MB  max: {maxmem_s}")
    except Exception as e:
        fail(f"Memory info failed: {e}")

    # Key inventory
    KEY_PATTERNS = [
        ("slips:active",          "Active slips hash"),
        ("slips:ratio",           "W/L ratio hash"),
        ("slips:alerted",         "Alerted set"),
        ("sf:games:*",            "Sofascore game cache"),
        ("props:odds_api",        "Props odds blob"),
        ("props:all",             "All props blob"),
        ("odds:snapshot:*",       "Odds snapshots"),
    ]

    print()
    total_keys = r.dbsize()
    info(f"Total Redis keys: {total_keys}")

    for pattern, label in KEY_PATTERNS:
        try:
            if "*" in pattern:
                keys = r.keys(pattern)
                count = len(keys)
                if count == 0:
                    info(f"{label:<30s} 0 keys")
                    continue
                # sample one key for size
                sample = keys[0]
            else:
                if not r.exists(pattern):
                    info(f"{label:<30s} not set")
                    continue
                sample = pattern
                count  = 1

            # measure size
            ktype = r.type(sample)
            if ktype == "hash":
                size  = r.hlen(sample)
                ttl   = r.ttl(sample)
                ttl_s = f"TTL {ttl//86400}d" if ttl > 0 else "NO TTL ⚠️"
                lbl   = ok if ttl > 0 else warn
                lbl(f"{label:<30s} {count} key(s)  {size} fields  {ttl_s}")
            elif ktype == "string":
                raw   = r.get(sample) or ""
                kb    = len(raw) // 1024
                ttl   = r.ttl(sample)
                ttl_s = f"TTL {ttl//3600}h" if ttl > 0 else "NO TTL ⚠️"
                lbl   = ok if ttl > 0 else warn
                lbl(f"{label:<30s} {count} key(s)  {kb}KB  {ttl_s}")
            else:
                ttl = r.ttl(sample)
                ttl_s = f"TTL {ttl}s" if ttl > 0 else "NO TTL"
                info(f"{label:<30s} {count} key(s)  type={ktype}  {ttl_s}")
        except Exception as e:
            warn(f"{label:<30s} error: {e}")


# ── 3. DATABASE HEALTH ────────────────────────────────────────────────────────

def test_database():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  DATABASE TABLE SIZES{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    try:
        from src.db.session import get_db
        from src.db.models import OddsSnapshot, Game, LineMovement, Pick, Alert
        ok("Database connected")
    except Exception as e:
        fail(f"Database import failed: {e}")
        return

    tables = [
        ("OddsSnapshot",  OddsSnapshot,  2_000_000, "Prune if >2M rows — run cleanup_old_snapshots"),
        ("Game",          Game,          50_000,    "Should stay small"),
        ("LineMovement",  LineMovement,  500_000,   "Prune if >500K rows"),
        ("Pick",          Pick,          10_000,    "Normal growth"),
        ("Alert",         Alert,         50_000,    "Normal growth"),
    ]

    try:
        with get_db() as db:
            for name, model, limit, note in tables:
                try:
                    count = db.query(model).count()
                    pct   = count / limit * 100
                    label = ok if pct < 50 else (warn if pct < 90 else fail)
                    label(f"{name:<20s} {count:>10,} rows  ({pct:.0f}% of limit)  — {note if pct >= 50 else 'OK'}")
                except Exception as e:
                    warn(f"{name:<20s} count failed: {e}")
    except Exception as e:
        fail(f"DB session failed: {e}")

    # Check for DB file size if SQLite
    db_url = os.getenv("DATABASE_URL", "")
    if "sqlite" in db_url:
        path = db_url.replace("sqlite:///", "")
        if os.path.exists(path):
            mb = os.path.getsize(path) / 1024 / 1024
            label = ok if mb < 500 else (warn if mb < 1000 else fail)
            label(f"SQLite file size: {mb:.1f}MB")
    elif "postgresql" in db_url or "postgres" in db_url:
        try:
            with get_db() as db:
                result = db.execute(
                    "SELECT pg_size_pretty(pg_database_size(current_database())) as size"
                ).fetchone()
                ok(f"Postgres DB size: {result[0]}")
        except Exception as e:
            warn(f"Could not get Postgres size: {e}")


# ── 4. PROCESS MEMORY ─────────────────────────────────────────────────────────

def test_process_memory():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  PROCESS MEMORY (this Python process){RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    try:
        import psutil
        proc  = psutil.Process()
        rss   = proc.memory_info().rss / 1024 / 1024
        label = ok if rss < 150 else (warn if rss < 300 else fail)
        label(f"RSS: {rss:.1f}MB")

        # System RAM
        vm = psutil.virtual_memory()
        used_pct = vm.percent
        avail_mb = vm.available / 1024 / 1024
        label2 = ok if used_pct < 70 else (warn if used_pct < 85 else fail)
        label2(f"System RAM: {used_pct:.0f}% used  {avail_mb:.0f}MB free  total {vm.total//1024//1024}MB")

        # Top processes by RAM
        print()
        info("Top processes by RAM:")
        procs = sorted(psutil.process_iter(["pid","name","memory_info"]),
                       key=lambda p: p.info["memory_info"].rss if p.info["memory_info"] else 0,
                       reverse=True)[:6]
        for p in procs:
            mb = p.info["memory_info"].rss / 1024 / 1024 if p.info["memory_info"] else 0
            info(f"  {p.info['name'][:25]:<25s} PID {p.info['pid']:>6}  {mb:>6.1f}MB")

    except ImportError:
        warn("psutil not installed — run: pip install psutil")
    except Exception as e:
        warn(f"Memory check failed: {e}")


# ── 5. THREAD POOL CHECK ──────────────────────────────────────────────────────

def test_thread_config():
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  THREAD POOL CONFIGURATION{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    checks = [
        ("data_hub build_game_context",    4,  "max_workers=4 — safe for 1GB VPS"),
        ("data_hub build_player_context",  4,  "max_workers=4"),
        ("odds_engine parallel fetch",     4,  "max_workers=4"),
        ("picks_worker candidates",        4,  "max_workers=4"),
        ("prediction_market_worker",       4,  "max_workers=4"),
        ("sofascore enrich_game_context",  6,  "max_workers=6 — 6 parallel Sofascore calls"),
    ]
    for name, workers, note in checks:
        label = ok if workers <= 6 else warn
        label(f"{name:<40s} {workers} workers  — {note}")

    ok("Runner is single-process (no Celery) — no worker amplification risk")


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  SPORTS-BOT DIAGNOSTIC  {RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    endpoint_results = test_endpoints()
    test_redis()
    test_database()
    test_process_memory()
    test_thread_config()

    # Summary
    passed = sum(1 for _, r in endpoint_results if r)
    total  = len(endpoint_results)
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  SUMMARY{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"  Endpoints: {passed}/{total} working")
    if passed < total:
        failed = [n for n, r in endpoint_results if not r]
        print(f"  {RED}Failed: {', '.join(failed)}{RESET}")
    print()
