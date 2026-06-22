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

# Optional premium data source — degrades gracefully if not set
SPORTRADAR_API_KEY  = _optional("SPORTRADAR_API_KEY")
# Web search for borderline picks (Perplexity — perplexity.ai/api, ~$5/month)
PERPLEXITY_API_KEY  = _optional("PERPLEXITY_API_KEY")

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

# ── Season calendar — (month_start, day_start, month_end, day_end) ─────────────
# Sports auto-enable/disable based on today's date. No manual changes needed.
_SEASON_CALENDAR: dict[str, tuple[int, int, int, int]] = {
    # US Sports
    "baseball_mlb":                         (3, 20,  10, 31),  # late Mar – Oct
    "basketball_nba":                       (10, 1,   6, 30),  # Oct – Jun
    "basketball_wnba":                      (5,  1,  10, 15),  # May – Oct
    "americanfootball_nfl":                 (9,  1,   2, 15),  # Sep – Feb
    "americanfootball_ncaaf":               (8, 25,   1, 15),  # Aug – Jan
    "basketball_ncaab":                     (11,  1,  4, 10),  # Nov – Apr
    "icehockey_nhl":                        (10,  1,  6, 30),  # Oct – Jun
    "icehockey_pwhl":                       (1,   1,  5, 31),  # Jan – May
    # Soccer — US
    "soccer_usa_mls":                       (2,  20, 11, 30),  # Feb – Nov
    "soccer_usa_nwsl":                      (3,   1, 11, 15),  # Mar – Nov
    # Soccer — Europe (Aug–May)
    "soccer_epl":                           (8,   1,  5, 31),
    "soccer_spain_la_liga":                 (8,   1,  5, 31),
    "soccer_germany_bundesliga":            (8,   1,  5, 31),
    "soccer_italy_serie_a":                 (8,   1,  5, 31),
    "soccer_france_ligue_one":              (8,   1,  5, 31),
    "soccer_netherlands_eredivisie":        (8,   1,  5, 31),
    "soccer_portugal_primeira_liga":        (8,   1,  5, 31),
    "soccer_spl":                           (8,   1,  5, 31),
    "soccer_turkey_super_league":           (8,   1,  5, 31),
    # Soccer — South America / Mexico (year-round with breaks)
    "soccer_brazil_campeonato":             (4,   1, 12, 10),  # Apr – Dec
    "soccer_argentina_primera_division":    (1,   1, 12, 31),  # year-round
    "soccer_mexico_ligamx":                 (1,   1, 12, 31),  # year-round
    "soccer_conmebol_copa_libertadores":    (2,   1, 11, 30),  # Feb – Nov
    "soccer_conmebol_copa_sudamericana":    (2,   1, 11, 30),  # Feb – Nov
    # Soccer — International cups (specific windows)
    "soccer_fifa_club_world_cup":           (6,   1,  7, 20),  # Jun–Jul 2026
    "soccer_fifa_world_cup":               (11,   1, 12, 31),  # Nov–Dec (2026 edition)
    "soccer_conmebol_copa_america":         (6,   1,  7, 31),  # Jun–Jul
    "soccer_africa_cup_of_nations":         (1,   1,  2, 28),  # Jan–Feb
    "soccer_uefa_champs_league":            (9,   1,  6,  1),  # Sep – May
    "soccer_uefa_europa_league":            (9,   1,  5, 31),
    "soccer_uefa_europa_conference_league": (9,   1,  5, 31),
    # Combat — year-round
    "mma_mixed_martial_arts":              (1,   1, 12, 31),
    "boxing_boxing":                       (1,   1, 12, 31),
    # Tennis — Grand Slams + tour events
    "tennis_atp_australian_open":          (1,   1,  2,  5),
    "tennis_wta_aus_open_singles":         (1,   1,  2,  5),
    "tennis_atp_french_open":              (5,  20,  6, 10),
    "tennis_wta_french_open":              (5,  20,  6, 10),
    "tennis_atp_queens_club_champ":        (6,   1,  6, 25),
    "tennis_atp_halle_open":               (6,   1,  6, 25),
    "tennis_wta_german_open":              (6,   1,  6, 25),
    "tennis_atp_wimbledon":                (6,  25,  7, 15),
    "tennis_wta_wimbledon":                (6,  25,  7, 15),
    "tennis_atp_us_open":                  (8,  25,  9, 10),
    "tennis_wta_us_open":                  (8,  25,  9, 10),
    # Golf — major season Apr–Jul
    "golf_pga_tour":                       (1,   1, 12, 31),  # year-round
    "golf_masters_tournament_winner":      (4,   1,  4, 15),
    "golf_pga_championship_winner":        (5,   1,  5, 25),
    "golf_us_open_winner":                 (6,   1,  6, 25),
    "golf_the_open_championship_winner":   (7,   1,  7, 25),
    "golf_lpga":                           (1,   1, 12, 31),
    # Aussie Rules / Rugby
    "aussierules_afl":                     (3,   1,  9, 30),  # Mar – Sep
    "rugbyleague_nrl":                     (3,   1, 10, 15),  # Mar – Oct
    "rugbyleague_nrl_state_of_origin":     (5,   1,  7, 31),  # May – Jul
    # Cricket
    "cricket_international_t20":           (1,   1, 12, 31),  # year-round
    "cricket_odi":                         (1,   1, 12, 31),
    "cricket_test_match":                  (1,   1, 12, 31),
    "cricket_ipl":                         (3,  15,  6,  1),  # Mar – May/Jun
}

# All sport key aliases (name → api_key) — full list kept here for reference
_ALL_SPORTS: dict[str, str] = {
    "nba":               "basketball_nba",
    "nfl":               "americanfootball_nfl",
    "mlb":               "baseball_mlb",
    "nhl":               "icehockey_nhl",
    "ncaab":             "basketball_ncaab",
    "ncaaf":             "americanfootball_ncaaf",
    "wnba":              "basketball_wnba",
    "pwhl":              "icehockey_pwhl",
    "mls":               "soccer_usa_mls",
    "nwsl":              "soccer_usa_nwsl",
    "epl":               "soccer_epl",
    "soccer":            "soccer_epl",
    "laliga":            "soccer_spain_la_liga",
    "bundesliga":        "soccer_germany_bundesliga",
    "seriea":            "soccer_italy_serie_a",
    "ligue1":            "soccer_france_ligue_one",
    "eredivisie":        "soccer_netherlands_eredivisie",
    "portugal":          "soccer_portugal_primeira_liga",
    "scotland":          "soccer_spl",
    "turkey":            "soccer_turkey_super_league",
    "ligamx":            "soccer_mexico_ligamx",
    "argentina":         "soccer_argentina_primera_division",
    "brazil":            "soccer_brazil_campeonato",
    "ucl":               "soccer_uefa_champs_league",
    "champions":         "soccer_uefa_champs_league",
    "europa":            "soccer_uefa_europa_league",
    "conferenceleague":  "soccer_uefa_europa_conference_league",
    "worldcup":          "soccer_fifa_world_cup",
    "cwc":               "soccer_fifa_club_world_cup",
    "clubworldcup":      "soccer_fifa_club_world_cup",
    "copaamerica":       "soccer_conmebol_copa_america",
    "conmebol":          "soccer_conmebol_copa_libertadores",
    "copalibertadores":  "soccer_conmebol_copa_libertadores",
    "copasudamericana":  "soccer_conmebol_copa_sudamericana",
    "afcon":             "soccer_africa_cup_of_nations",
    "ufc":               "mma_mixed_martial_arts",
    "mma":               "mma_mixed_martial_arts",
    "boxing":            "boxing_boxing",
    "tennis":            "tennis_atp_wimbledon",
    "atp":               "tennis_atp_wimbledon",
    "queens":            "tennis_atp_queens_club_champ",
    "halle":             "tennis_atp_halle_open",
    "wta":               "tennis_wta_german_open",
    "wimbledon":         "tennis_atp_wimbledon",
    "wwimbledon":        "tennis_wta_wimbledon",
    "usopen":            "tennis_atp_us_open",
    "wusopen":           "tennis_wta_us_open",
    "ausopen":           "tennis_atp_australian_open",
    "wausopen":          "tennis_wta_aus_open_singles",
    "frenchopen":        "tennis_atp_french_open",
    "wfrenchopen":       "tennis_wta_french_open",
    "golf":              "golf_pga_tour",
    "pga":               "golf_pga_tour",
    "masters":           "golf_masters_tournament_winner",
    "pgachamp":          "golf_pga_championship_winner",
    "usopen_golf":       "golf_us_open_winner",
    "theopen":           "golf_the_open_championship_winner",
    "lpga":              "golf_lpga",
    "afl":               "aussierules_afl",
    "nrl":               "rugbyleague_nrl",
    "stateoforigin":     "rugbyleague_nrl_state_of_origin",
    "t20":               "cricket_international_t20",
    "odi":               "cricket_odi",
    "cricket":           "cricket_test_match",
    "ipl":               "cricket_ipl",
}


def _is_in_season(api_key: str) -> bool:
    """Return True if today's date falls within the sport's active season."""
    from datetime import date
    season = _SEASON_CALENDAR.get(api_key)
    if not season:
        return False  # not in calendar = never scan
    ms, ds, me, de = season
    today = date.today()
    m, d = today.month, today.day
    start = (ms, ds)
    end   = (me, de)
    if start <= end:
        return start <= (m, d) <= end
    # Wraps year boundary (e.g. Oct–Feb)
    return (m, d) >= start or (m, d) <= end


def _build_active_sports() -> dict[str, str]:
    """Return only the alias→api_key pairs whose season is active today."""
    active_keys = {v for v in _ALL_SPORTS.values() if _is_in_season(v)}
    return {alias: key for alias, key in _ALL_SPORTS.items() if key in active_keys}


# ── Sports tracked — auto-filtered to in-season only ──────────────────────────
SPORTS = _build_active_sports()

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
# Rule: bypass if (a) key-authenticated API, or (b) free public API that blocks proxies
PROXY_BYPASS_HOSTS = {
    "api.the-odds-api.com",
    "external-api.kalshi.com",
    "api.openai.com",
    "api.sleeper.app",
    # site.api.espn.com removed — ESPN blocks VPS datacenter IPs; route through Decodo residential proxy
    "api.underdogfantasy.com",   # works direct — blocks proxy IPs
    "api.sportradar.com",        # key-authenticated — bypass proxy
    "api.perplexity.ai",         # key-authenticated — bypass proxy
}

# ── OpenAI ─────────────────────────────────────────────────────────────────────
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MAX_TOKENS = 2048

