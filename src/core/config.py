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
def _require(name: str) -> str:
    """Fail loudly at startup if a required secret is missing — never use a hardcoded default."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not set. "
            "Add it to your .env file or deployment secrets manager."
        )
    return value

def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

# Keys that are always required (no fallback — fail fast rather than silently use wrong key)
OPENAI_API_KEY      = _require("OPENAI_API_KEY")
ODDS_API_KEY        = _require("ODDS_API_KEY")

# Discord — webhook URL only; no bot token or guild ID needed
DISCORD_WEBHOOK_URL = _optional("DISCORD_WEBHOOK_URL")

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL    = os.getenv("DATABASE_URL", "")
REDIS_URL       = os.getenv("REDIS_URL",       "redis://localhost:6379/0")

# ── Fallback SQLite for development without Postgres ──────────────────────────
SQLITE_URL = f"sqlite:///{BASE_DIR}/data/sip.db"
USE_SQLITE = os.getenv("USE_SQLITE", "true").lower() == "true"
EFFECTIVE_DB_URL = SQLITE_URL if USE_SQLITE else DATABASE_URL

# SQLite does not support concurrent writes — it will corrupt data under load.
# Refuse to start with SQLite in production.
if USE_SQLITE and ENVIRONMENT == "production":
    raise RuntimeError(
        "USE_SQLITE=true is not allowed in ENVIRONMENT=production. "
        "Set USE_SQLITE=false and configure DATABASE_URL to a PostgreSQL instance."
    )

if not USE_SQLITE and not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL must be set when USE_SQLITE=false. "
        "Add DATABASE_URL=postgresql://user:pass@host:5432/dbname to your .env file."
    )

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
    "worldcup": "soccer_fifa_world_cup",
    "wc":       "soccer_fifa_world_cup",
    "tennis":   "tennis_atp_french_open",
    "ufc":      "mma_mixed_martial_arts",
    "mma":      "mma_mixed_martial_arts",
    # golf removed — US Open not yet in Odds API, add back when available
}

SPORTSBOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet", "espnbet", "hardrock"]

# ── Betting apps (optional — platform degrades gracefully if not set) ──────────
KALSHI_API_KEY_ID  = _optional("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = _optional("KALSHI_PRIVATE_KEY")
# PrizePicks, Underdog, HardRock — no key required

# ── Decodo Residential Proxy ───────────────────────────────────────────────────
# Format: http://username:password@gate.decodo.com
# Ports 10001-10010 are rotated automatically per request.
# Leave blank to disable proxy (direct connection).
DECODO_PROXY_URL = _optional("DECODO_PROXY_URL")   # e.g. http://user:pass@gate.decodo.com

# Domains that must NEVER go through the proxy
# Rule: bypass if (a) key-authenticated API where proxy adds no value, or (b) free public API that blocks proxies
PROXY_BYPASS_HOSTS = {
    "api.openai.com",              # key-authenticated, latency-sensitive
    "api.sleeper.app",             # free public API — no auth, proxy causes 502
    "site.api.espn.com",           # ESPN public API — no auth needed
    "api.the-odds-api.com",        # key-authenticated — proxy adds latency, no scraping needed
}

# ── OpenAI ─────────────────────────────────────────────────────────────────────
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MAX_TOKENS = 2048

