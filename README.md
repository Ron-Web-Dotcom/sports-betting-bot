# Sports Betting Bot

An AI-powered sports betting bot with multi-platform support, Kelly Criterion sizing, paper trading, Discord alerts, and VPS deployment via systemd.

---

## Architecture

```
bot.py                        ← main orchestrator loop
config/settings.py            ← all config from .env
src/
  apis/
    odds_api.py               ← The Odds API (live lines, all sports)
    espn.py                   ← ESPN (injuries, scores, news)
    prizepicks.py             ← PrizePicks player props
    underdog.py               ← Underdog Fantasy lines
    sleeper.py                ← Sleeper (player data, trending)
    draftkings.py             ← DraftKings live odds
    fanduel.py                ← FanDuel live odds
  core/
    ai_engine.py              ← Claude AI analysis + decisions
    risk_manager.py           ← Kelly sizing + guardrails
    paper_trader.py           ← Paper/live trade execution
    opportunity_finder.py     ← Aggregates all sources per event
    database.py               ← SQLite persistence
  alerts/
    discord.py                ← Discord webhook notifications
tests/                        ← Full pytest suite
deployment/
  sports-betting-bot.service  ← systemd unit file
  install.sh                  ← one-command VPS install
```

---

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/ron-web-dotcom/sports-betting-bot
cd sports-betting-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — add your API keys
```

Required keys:
| Variable | Source |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |
| `ODDS_API_KEY` | https://the-odds-api.com |
| `DISCORD_WEBHOOK_URL` | Discord → Server Settings → Integrations → Webhooks |

### 3. Run in paper trading mode

```bash
python bot.py
```

### 4. Run tests

```bash
pytest tests/ -v
```

---

## Deployment on VPS

```bash
# As root on your VPS
bash deployment/install.sh
nano /opt/sports-betting-bot/.env     # add API keys
systemctl start sports-betting-bot
journalctl -u sports-betting-bot -f   # tail logs
```

---

## How It Works

1. **Every 5 minutes** the bot fetches live odds from The Odds API across 10+ sports
2. **ESPN** provides injury reports and news for each sport
3. **Opportunity finder** cross-references lines from PrizePicks, Underdog, DraftKings, FanDuel
4. **Claude AI** analyses each event with injury context and cross-platform line discrepancies to find edge
5. **Kelly sizing** calculates optimal stake (capped at 25% Kelly and 5% of bankroll)
6. Bet is recorded in **SQLite** (paper mode) with full audit trail
7. **Discord alert** fires in plain English with reasoning, key factors, and sizing details
8. Completed games are **auto-settled** on the next scan cycle

---

## Risk Management

- Paper trading mode ON by default — flip `PAPER_TRADING=false` only when ready
- Minimum 60% AI confidence required (`MIN_CONFIDENCE`)
- Minimum 3% edge required (`MIN_EDGE_THRESHOLD`)
- Fractional Kelly at 25% (`MAX_KELLY_FRACTION`)
- Hard cap: never bet more than 5% of bankroll (`MAX_BET_PCT`)
- Bankroll tracked in SQLite with full history

---

## Supported Platforms

| Platform | Type | Data Used |
|---|---|---|
| The Odds API | Sportsbook aggregator | Live lines (h2h, spreads, totals) |
| PrizePicks | DFS props | Player prop lines |
| Underdog Fantasy | DFS props | Over/under lines |
| Sleeper | DFS / fantasy | Player status & trending |
| DraftKings | Sportsbook | Live odds |
| FanDuel | Sportsbook | Live odds |
| ESPN | Stats/news | Injuries, scores, news |

---

## Supported Sports

NFL, NCAAF, NBA, NCAAB, MLB, NHL, EPL, UEFA Champions League, ATP Tennis, MMA
