"""Central configuration — all values read from environment / .env file."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

# ── API Keys ──────────────────────────────────────────────────────────────────
ODDS_API_KEY        = os.getenv("ODDS_API_KEY", "2abd34975bfe02e0ce58cd8410450f79")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1511150608185561118/CfcL7QAa7zwDuxIQ3U0xG-oamLypdx2yYkE_xhFQrFS9mWO_KySrItLb1nzVLpgVG-sx",
)

# Platform credentials (public endpoints used where auth not required)
PRIZEPICKS_API_BASE  = "https://api.prizepicks.com"
UNDERDOG_API_BASE    = "https://api.underdogfantasy.com"
SLEEPER_API_BASE     = "https://api.sleeper.app/v1"
DRAFTKINGS_API_BASE  = "https://sportsbook.draftkings.com"
FANDUEL_API_BASE     = "https://sbapi.fanduel.com"
ODDS_API_BASE        = "https://api.the-odds-api.com/v4"
ESPN_API_BASE        = "https://site.api.espn.com/apis/site/v2/sports"

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "betting_bot.db"))

# ── Trading ───────────────────────────────────────────────────────────────────
PAPER_TRADING        = os.getenv("PAPER_TRADING", "true").lower() == "true"
BANKROLL             = float(os.getenv("BANKROLL", "1000.0"))
MAX_KELLY_FRACTION   = float(os.getenv("MAX_KELLY_FRACTION", "0.25"))
MAX_BET_PCT          = float(os.getenv("MAX_BET_PCT", "0.05"))
MIN_EDGE_THRESHOLD   = float(os.getenv("MIN_EDGE_THRESHOLD", "0.03"))
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", "0.60"))

# ── Scheduler ─────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))

# ── Sports to track ───────────────────────────────────────────────────────────
TRACKED_SPORTS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "basketball_nba",
    "basketball_ncaab",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "tennis_atp_french_open",
    "mma_mixed_martial_arts",
]

ESPN_SPORT_MAP = {
    "americanfootball_nfl":  ("football",  "nfl"),
    "americanfootball_ncaaf":("football",  "college-football"),
    "basketball_nba":        ("basketball","nba"),
    "basketball_ncaab":      ("basketball","mens-college-basketball"),
    "baseball_mlb":          ("baseball",  "mlb"),
    "icehockey_nhl":         ("hockey",    "nhl"),
    "soccer_epl":            ("soccer",    "eng.1"),
    "soccer_uefa_champs_league":("soccer", "uefa.champions"),
}

# ── Claude model ──────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-opus-4-8"
CLAUDE_MAX_TOKENS = 1024
