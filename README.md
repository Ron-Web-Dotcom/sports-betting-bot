# Sports Betting Bot

A Kalshi-style automated sports betting system with parlay construction, arbitrage detection, live hedging, multi-factor signal engine, and a real-time CLI dashboard.

---

## Architecture

```
bot.py                          <- Entry point + signal handler
config/settings.py              <- All config from .env
src/
  apis/
    odds_api.py                 <- The Odds API — live lines for 10 sports
    espn.py                     <- ESPN — injuries, scores, news
    prizepicks.py               <- PrizePicks player props
    underdog.py                 <- Underdog Fantasy lines
    sleeper.py                  <- Sleeper player status/trending
    draftkings.py               <- DraftKings live odds
    fanduel.py                  <- FanDuel live odds
  core/
    market_scanner.py           <- Event-driven scan loop (60s/15s for live)
    signal_engine.py            <- Multi-factor signal scoring (AI + line movement + injuries)
    ai_engine.py                <- Claude analysis — legs, parlay approval, summaries
    parlay_builder.py           <- 2-4 leg parlay construction + SGP builder
    arb_detector.py             <- Cross-book arbitrage detection
    hedge_engine.py             <- Live parlay hedge calculations (full + partial)
    line_shopper.py             <- Best-line finder across all books
    position_manager.py         <- Portfolio tracker, settlement, hedge triggers
    risk_manager.py             <- Portfolio Kelly sizing + guardrails
    paper_trader.py             <- Execution engine (paper/live)
    database.py                 <- SQLite: positions, parlays, arbs, signals, bankroll
  alerts/
    discord.py                  <- Rich Discord embeds for all event types
  dashboard/
    cli.py                      <- Live Rich terminal UI
tests/                          <- 56 passing tests
deployment/
  sports-betting-bot.service    <- systemd unit (hardened)
  install.sh                    <- One-command VPS install
```

---

## How It Works

```
Every 60s (15s when live games detected):
  1. Fetch live odds (The Odds API, all sports)
  2. Save market snapshots for line-movement tracking
  3. Scan all events for cross-book arbitrage
  4. Settle completed positions via live scores
  5. Check open parlays for hedge opportunities
  6. For each event:
       - Ask Claude to evaluate the betting opportunity
       - Line-shop for best odds across all books
       - Compute multi-factor signal score (AI + line movement + injuries)
  7. Rank signals by composite score
  8. Place top straight bets (Kelly-sized, risk-checked)
  9. Build optimal 2-4 leg parlays from top signals
 10. Place top 2 parlay candidates
 11. Fire Discord alerts for every action
```

---

## Quick Start

```bash
git clone https://github.com/ron-web-dotcom/sports-betting-bot
cd sports-betting-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # keys already set

python bot.py           # full bot + live dashboard
python bot.py --no-dash # headless (systemd mode)
python bot.py --dash    # dashboard read-only view
```

---

## Commands

| Command | Description |
|---|---|
| `python bot.py` | Run bot + live terminal dashboard |
| `python bot.py --no-dash` | Headless mode (for systemd/VPS) |
| `python bot.py --dash` | Dashboard only (monitor without trading) |
| `pytest tests/ -v` | Run all 56 tests |

---

## VPS Deployment

```bash
bash deployment/install.sh
systemctl start sports-betting-bot
journalctl -u sports-betting-bot -f
```

---

## Risk Management

| Parameter | Default | Description |
|---|---|---|
| `PAPER_TRADING` | `true` | Paper mode on by default |
| `MAX_KELLY_FRACTION` | `0.25` | 25% fractional Kelly |
| `MAX_BET_PCT` | `0.05` | Max 5% bankroll per straight bet |
| `MAX_PARLAY_PCT` | `0.02` | Max 2% bankroll per parlay |
| `MAX_PORTFOLIO_EXPOSURE` | `0.20` | Max 20% bankroll at risk at once |
| `MIN_EDGE_THRESHOLD` | `0.03` | Min 3% EV to bet |
| `MIN_CONFIDENCE` | `0.60` | Min 60% AI confidence |
| `ARB_MIN_PROFIT_PCT` | `0.01` | Min 1% guaranteed arb profit |

---

## Discord Alerts

- **Straight bet placed** — event, selection, odds, stake, edge, AI reasoning, key factors
- **Parlay placed** — all legs, combined odds, stake, EV, win probability
- **Bet settled** — result, P&L, new bankroll
- **Arbitrage found** — both sides, books, stakes, guaranteed profit
- **Hedge opportunity** — full/partial hedge stakes, profit/loss scenarios
- **Session summary** — W/L, total P&L, arbs found (every 6 hours + on shutdown)

---

## Supported Sports

NFL, NCAAF, NBA, NCAAB, MLB, NHL, EPL, UEFA Champions League, MMA

## Supported Books

DraftKings, FanDuel, BetMGM, Caesars, PointsBet, Unibet (via The Odds API)

## Prop Platforms

PrizePicks, Underdog Fantasy, Sleeper
