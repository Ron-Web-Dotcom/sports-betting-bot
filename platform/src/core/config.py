"""Platform configuration — all settings from environment."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent

# ── Identity ───────────────────────────────────────────────────────────────────
PLATFORM_NAME    = "Sports Intelligence Platform"
PLATFORM_VERSION = "1.0.0"
ENVIRONMENT      = os.getenv("ENVIRONMENT", "development")  # development | staging | production

# ── API Keys ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "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")
ODDS_API_KEY         = os.getenv("ODDS_API_KEY",       "2abd34975bfe02e0ce58cd8410450f79")
DISCORD_BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN",  "")
DISCORD_GUILD_ID     = int(os.getenv("DISCORD_GUILD_ID", "0"))
DISCORD_WEBHOOK_URL  = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1511150608185561118/CfcL7QAa7zwDuxIQ3U0xG-oamLypdx2yYkE_xhFQrFS9mWO_KySrItLb1nzVLpgVG-sx")

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL    = os.getenv("DATABASE_URL",    "postgresql://sip:sip_pass@localhost:5432/sip_db")
REDIS_URL       = os.getenv("REDIS_URL",       "redis://localhost:6379/0")
CELERY_BROKER   = os.getenv("CELERY_BROKER",   "redis://localhost:6379/1")
CELERY_BACKEND  = os.getenv("CELERY_BACKEND",  "redis://localhost:6379/2")

# ── Fallback SQLite for development without Postgres ──────────────────────────
SQLITE_URL = f"sqlite:///{BASE_DIR}/data/sip.db"
USE_SQLITE = os.getenv("USE_SQLITE", "true").lower() == "true"
EFFECTIVE_DB_URL = SQLITE_URL if USE_SQLITE else DATABASE_URL

# ── External API bases ─────────────────────────────────────────────────────────
ODDS_API_BASE        = "https://api.the-odds-api.com/v4"
ESPN_API_BASE        = "https://site.api.espn.com/apis/site/v2/sports"
PRIZEPICKS_API_BASE  = "https://api.prizepicks.com"
UNDERDOG_API_BASE    = "https://api.underdogfantasy.com"
SLEEPER_API_BASE     = "https://api.sleeper.app/v1"

# ── Trading ────────────────────────────────────────────────────────────────────
PAPER_TRADING          = os.getenv("PAPER_TRADING", "true").lower() == "true"
DEFAULT_BANKROLL       = float(os.getenv("DEFAULT_BANKROLL", "1000.0"))
UNIT_SIZE_PCT          = float(os.getenv("UNIT_SIZE_PCT",    "0.01"))   # 1 unit = 1% bankroll
MAX_DAILY_UNITS        = float(os.getenv("MAX_DAILY_UNITS",  "15.0"))
MAX_SINGLE_UNITS       = float(os.getenv("MAX_SINGLE_UNITS", "5.0"))
MAX_SPORT_EXPOSURE_PCT = float(os.getenv("MAX_SPORT_EXPOSURE_PCT", "0.40"))
MIN_EV_PCT             = float(os.getenv("MIN_EV_PCT",       "0.01"))   # 1% min EV

# ── Unit thresholds ────────────────────────────────────────────────────────────
UNIT_TIERS = {
    1: (0.01, 0.03),   # Small Edge: EV 1-3%
    2: (0.03, 0.06),   # Good Edge: EV 3-6%
    3: (0.06, 0.10),   # Strong Edge: EV 6-10%
    4: (0.10, 0.15),   # Very Strong: EV 10-15%
    5: (0.15, 9.99),   # Elite Play: EV 15%+
}

# ── Scan intervals ─────────────────────────────────────────────────────────────
ODDS_SCAN_INTERVAL_SECONDS  = int(os.getenv("ODDS_SCAN_INTERVAL_SECONDS",  "60"))
NEWS_SCAN_INTERVAL_SECONDS  = int(os.getenv("NEWS_SCAN_INTERVAL_SECONDS",  "120"))
CLV_CALC_INTERVAL_SECONDS   = int(os.getenv("CLV_CALC_INTERVAL_SECONDS",   "3600"))
PORTFOLIO_BUILD_HOUR        = int(os.getenv("PORTFOLIO_BUILD_HOUR",         "8"))    # 8 AM daily

# ── Pre-game alert windows (minutes before tip) ────────────────────────────────
PREGAME_ALERT_WINDOWS = [60, 30, 15, 10, 5, 0]

# ── Sports tracked ─────────────────────────────────────────────────────────────
SPORTS = {
    "nba":      "basketball_nba",
    "nfl":      "americanfootball_nfl",
    "mlb":      "baseball_mlb",
    "nhl":      "icehockey_nhl",
    "soccer":   "soccer_epl",
    "tennis":   "tennis_atp_french_open",
    "ufc":      "mma_mixed_martial_arts",
    "mma":      "mma_mixed_martial_arts",
    "boxing":   "boxing_boxing",
    "golf":     "golf_masters_tournament_winner",
    "f1":       "motorsport_formula_1",
    "cricket":  "cricket_icc_world_cup",
    "esports":  "esports_lol",
    "ncaab":    "basketball_ncaab",
    "ncaaf":    "americanfootball_ncaaf",
}

SPORTSBOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet", "espnbet", "hardrock"]

# ── Claude ─────────────────────────────────────────────────────────────────────
CLAUDE_MODEL      = "claude-opus-4-8"
CLAUDE_MAX_TOKENS = 2048

# ── Discord channel names ──────────────────────────────────────────────────────
DISCORD_CHANNELS = {
    "top_picks":      "top-picks",
    "new_odds":       "new-odds",
    "line_movement":  "line-movement",
    "player_props":   "player-props",
    "parlays":        "parlays",
    "soccer":         "soccer",
    "nba":            "nba",
    "nfl":            "nfl",
    "mlb":            "mlb",
    "nhl":            "nhl",
    "mma":            "mma",
    "tennis":         "tennis",
    "golf":           "golf",
    "esports":        "esports",
    "live_tracker":   "live-tracker",
    "game_alerts":    "game-alerts",
    "results":        "results",
    "daily_summary":  "daily-summary",
    "weekly_summary": "weekly-summary",
    "monthly_summary":"monthly-summary",
    "discussion":     "discussion",
}
