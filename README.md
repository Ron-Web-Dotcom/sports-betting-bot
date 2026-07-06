# Sports Betting Bot

An automated sports intelligence system that generates high-confidence picks for HardRock and Kalshi, posts them to Discord, tracks slips end-to-end, and settles results automatically.

---

## The Main Flow

This is the one flow the bot follows every day. Nothing else.

```
8:00 AM ET ── Sofascore full scan
               └── get ALL today's live events (times + odds)
                   ├── DAY games   → kickoff before 4 PM ET  → saved to Redis
                   └── NIGHT games → kickoff 4 PM ET+        → saved to Redis

10:30 AM ET ── HardRock DAY entry  *(paused until July 10)*
               └── load day games from Sofascore cache
                   └── Odds API → moneylines / spreads / totals / props
                       └── AI scores each pick
                           └── conf ≥ 76.5% + EV ≥ 0.5% → pick best 2
                               └── POST slip to Discord → save to Redis + DB

10:35 AM ET ── Kalshi DAY entry
               └── Kalshi API → all open YES/NO markets for day games
                   └── Sofascore enriches each market (kickoff, odds, form, H2H)
                       └── Odds API fallback if Kalshi API is empty
                           └── AI picks the single best contract
                               └── conf ≥ 76.5% + EV ≥ 0.5% → pick best 1
                                   └── POST slip to Discord → save to Redis + DB

 3:00 PM ET ── Sofascore rescan  (catches postponements / time changes)

 4:30 PM ET ── HardRock NIGHT entry  *(same flow, uses night games)*
 4:35 PM ET ── Kalshi NIGHT entry    *(same flow, uses night games)*

Every 3 min ── Slip tracker  (runs for BOTH day and night slips)
               ├── GAME SOON → alert 5–45 min before kickoff
               ├── GAME LIVE → alert when Sofascore shows inprogress
               └── RESULT    → Sofascore finished → Kalshi API → Odds API
                   ├── all legs won → CASHED ✅
                   └── any leg lost → DEAD ❌
```

**That's it. Sofascore feeds the schedule. Odds API + Kalshi feed the prices. AI picks the best bet. Slip tracker watches every game until it's settled.**

---

## Hard Rules — Never Change

| Rule | Value |
|------|-------|
| Confidence floor | **77%+** (`CONF_FLOOR = 0.765`) |
| EV floor | **0.5%+** (`EV_FLOOR = 0.005`) |
| HardRock pick cap | **2 legs max** |
| Kalshi pick cap | **1 leg max** |
| Bet outcomes | **CASHED or DEAD only** — no push, no draw, no void |
| Prediction markets | **Kalshi only** — no Polymarket |
| HardRock entries | **Paused until July 10, 2026** |
| Exact spread / total / prop line | **LOST** (not push) |

---

## Daily Schedule (Eastern Time)

| Time ET | Task |
|---------|------|
| 2:00 AM | Self-improvement cycle |
| 2:50 AM | Weekly summary (Sunday only) |
| 2:52 AM | Cleanup old slips (8-day window) |
| **3:00 AM** | **Sleep mode — bot goes quiet** |
| **5:00 AM** | **Wake-up brief — daily summary to Discord** |
| 5:30 AM | Refresh active sports |
| 6:00 AM | Yesterday's recap |
| **8:00 AM** | **Sofascore full scan — day/night split (4 PM ET cutoff)** |
| 10:30 AM | HardRock day entry *(paused until July 10)* |
| **10:35 AM** | **Kalshi day entry** |
| 12:00 PM | Health check |
| **3:00 PM** | **Sofascore rescan — catch postponements / time changes** |
| 4:30 PM | HardRock night entry *(paused until July 10)* |
| **4:35 PM** | **Kalshi night entry** |

**Every 3 min:** Slip tracker — Game Soon (30 min before tip) · Live Now · CASHED / DEAD

**Every 30 min:** Odds scan — 5 AM to 3 AM ET (skips sleep window)

---

## Workers

| Worker | File | Role |
|--------|------|------|
| Picks | `src/workers/picks_worker.py` | HardRock day/night entries |
| Prediction Market | `src/workers/prediction_market_worker.py` | Kalshi day/night entries |
| Slip Tracker | `src/workers/slip_tracker.py` | Game Soon / Live / CASHED / DEAD alerts — Sofascore → Kalshi → Odds API for results |
| Settlement | `src/workers/settlement_worker.py` | Settles picks via Odds API (days_from=14) |
| Analytics | `src/workers/analytics_worker.py` | Summaries, sleep/wake, self-improvement |
| Odds | `src/workers/odds_worker.py` | Odds snapshots + line movement |

---

## Data Sources (The Trunk)

| Source | File | Provides |
|--------|------|----------|
| Sofascore | `src/apis/sofascore.py` | Schedules, live status, scores, results, H2H, form — **primary result source** |
| Odds API | `src/engines/odds_engine.py` | Moneylines, spreads, totals, props, scores (fallback for results) |
| Kalshi | `src/apis/kalshi.py` | Prediction market prices and settlement (fallback for results) |
| TheSportsDB | `src/apis/thesportsdb.py` | Team form, H2H history, player bios |
| Action Network | `src/apis/action_network.py` | Public betting % and sharp action signals |

---

## Engines

| Engine | File | Role |
|--------|------|------|
| Pick Gate | `src/engines/pick_gate.py` | Final filter — conf ≥ 76.5%, EV ≥ 3%, risk ≤ 75 |
| Prop Engine | `src/engines/prop_engine.py` | Over/under grading — exact line = LOST |
| Summary Engine | `src/engines/summary_engine.py` | Daily/weekly P&L |
| Portfolio Engine | `src/engines/portfolio_engine.py` | Risk-tiered portfolio construction |
| Expiration Engine | `src/engines/expiration_engine.py` | Bet urgency from line movement |
| Line Movement | `src/engines/line_movement_engine.py` | Steam / sharp / public money detection |

---

## Redis Keys

| Key | TTL | Content |
|-----|-----|---------|
| `sofascore:day_games` | 24h | Games kicking off before 4 PM ET |
| `sofascore:night_games` | 24h | Games kicking off 4 PM ET or later |
| `sofascore:today_index` | 24h | Team-name → event lookup |
| `sofascore:today_events` | 24h | Full today event list for team-name lookup |
| `sofascore:event:{id}` | 2 min | Per-event live status cache |
| `slips:active` | persist | Active slips — never auto-expires |
| `kalshi:posted:{period}:{date}` | 24h | Dedup guard — one entry per period per day |

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Required environment variables
OPENAI_API_KEY=...
ODDS_API_KEY=...
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY=...
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER=redis://localhost:6379/1
CELERY_BACKEND=redis://localhost:6379/2
DATABASE_URL=postgresql://...
DISCORD_BOT_TOKEN=...
DISCORD_CHANNEL_ID=...

# Start
python3 runner.py

# Manage via systemd
sudo systemctl restart sports-bot
sudo systemctl status sports-bot
```

---

## CI

GitHub Actions runs on every push to `main`:

- **Lint** — `ruff check src/` — zero tolerance
- **Tests** — 537 tests including 22 invariant tests that enforce all hard rules

```bash
# Run locally before pushing
ruff check src/
python3 -m pytest tests/ -q
# Expected: 537 passed, 3 skipped (celery stubs — intentional)
```

---

## Invariant Tests (`tests/test_invariants.py`)

These 22 tests enforce the hard rules at the code level — CI fails if any are broken:

- `CONF_FLOOR` never below 0.765
- `EV_FLOOR` never below 0.005
- HardRock cap = 2 picks, Kalshi cap = 1 pick
- No Polymarket anywhere in source
- Exact line = LOST in prop engine, settlement, spreads, totals
- Slip key uses `persist()` not `expire()`
- Settlement uses `days_from >= 14`
- Sleep window consistent at 3–5 AM ET across all workers
- HardRock paused until July 10, 2026
