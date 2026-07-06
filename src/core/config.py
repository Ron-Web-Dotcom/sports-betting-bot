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
CELERY_BROKER   = os.getenv("CELERY_BROKER",   "redis://localhost:6379/1")
CELERY_BACKEND  = os.getenv("CELERY_BACKEND",  "redis://localhost:6379/2")

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

# ── Season calendar — fallback only, used when Odds API is unreachable ──────────
# Primary source of truth is the Odds API /sports endpoint (cached 6h in Redis).
# These dates are approximate and used as a last resort only.
_SEASON_CALENDAR: dict[str, tuple[int, int, int, int]] = {
    # ── US Major Sports ───────────────────────────────────────────────────────
    "baseball_mlb":                              (3, 20, 10, 31),  # late Mar – Oct
    "basketball_nba":                            (10, 1,  6, 30),  # Oct – Jun
    "basketball_wnba":                           (5,  1, 10, 15),  # May – Oct
    "americanfootball_nfl":                      (9,  1,  2, 15),  # Sep – Feb
    "americanfootball_ncaaf":                    (8, 25,  1, 15),  # Aug – Jan
    "basketball_ncaab":                          (11, 1,  4, 10),  # Nov – Apr
    "basketball_ncaab_womens":                   (11, 1,  4, 10),  # Nov – Apr
    "icehockey_nhl":                             (10, 1,  6, 30),  # Oct – Jun
    "icehockey_pwhl":                            (1,  1,  5, 31),  # Jan – May
    # ── Women's US Sports ─────────────────────────────────────────────────────
    "basketball_wncaab":                         (11, 1,  4, 10),
    "americanfootball_nfl_super_bowl_winner":    (9,  1,  2, 15),
    # ── Soccer — US ───────────────────────────────────────────────────────────
    "soccer_usa_mls":                            (2, 20, 11, 30),  # Feb – Nov
    "soccer_usa_nwsl":                           (3,  1, 11, 15),  # Mar – Nov
    # ── Soccer — Europe Top 5 (Aug–May) ──────────────────────────────────────
    "soccer_epl":                                (8,  1,  5, 31),
    "soccer_spain_la_liga":                      (8,  1,  5, 31),
    "soccer_germany_bundesliga":                 (8,  1,  5, 31),
    "soccer_italy_serie_a":                      (8,  1,  5, 31),
    "soccer_france_ligue_one":                   (8,  1,  5, 31),
    # ── Soccer — Europe Other Leagues ─────────────────────────────────────────
    "soccer_netherlands_eredivisie":             (8,  1,  5, 31),
    "soccer_portugal_primeira_liga":             (8,  1,  5, 31),
    "soccer_spl":                                (8,  1,  5, 31),  # Scotland
    "soccer_turkey_super_league":                (8,  1,  5, 31),
    "soccer_belgium_first_div":                  (8,  1,  5, 31),
    "soccer_greece_super_league":                (8,  1,  5, 31),
    "soccer_denmark_superliga":                  (7, 15,  6, 15),  # Jul–Jun
    "soccer_sweden_allsvenskan":                 (4,  1, 11, 15),  # Apr–Nov
    "soccer_norway_eliteserien":                 (4,  1, 11, 15),
    "soccer_finland_veikkausliiga":              (4,  1, 11, 15),
    "soccer_austria_bundesliga":                 (7, 15,  5, 31),
    "soccer_swiss_superleague":                  (7, 15,  5, 31),
    "soccer_czech_liga":                         (7, 15,  5, 31),
    "soccer_poland_ekstraklasa":                 (7, 15,  5, 31),
    "soccer_romania_liga_1":                     (7, 15,  5, 31),
    "soccer_croatia_hnl":                        (7, 15,  5, 31),
    # ── Soccer — Women's Europe ───────────────────────────────────────────────
    "soccer_england_wsl":                        (9,  1,  5, 31),
    "soccer_germany_frauen_bundesliga":          (9,  1,  5, 31),
    "soccer_spain_liga_f":                       (9,  1,  5, 31),
    "soccer_france_d1_feminine":                 (9,  1,  5, 31),
    "soccer_italy_serie_a_feminine":             (9,  1,  5, 31),
    "soccer_uefa_womens_champs_league":          (10, 1,  5, 31),
    # ── Soccer — South America / Mexico ──────────────────────────────────────
    "soccer_brazil_campeonato":                  (4,  1, 12, 10),
    "soccer_argentina_primera_division":         (1,  1, 12, 31),  # year-round
    "soccer_mexico_ligamx":                      (1,  1, 12, 31),  # year-round
    "soccer_chile_primera_division":             (2,  1, 12, 15),
    "soccer_colombia_primera_a":                 (1, 20, 12, 15),
    "soccer_ecuador_liga_pro":                   (2,  1, 12, 15),
    "soccer_uruguay_primera_division":           (2,  1, 12, 15),
    "soccer_peru_primera_division":              (2,  1, 12, 15),
    "soccer_venezuela_primera_liga":             (1,  1, 12, 31),
    "soccer_conmebol_copa_libertadores":         (2,  1, 11, 30),
    "soccer_conmebol_copa_sudamericana":         (2,  1, 11, 30),
    # ── Soccer — Asia / Middle East / Africa ─────────────────────────────────
    "soccer_japan_j_league":                     (2, 15, 12,  1),
    "soccer_south_korea_kleague1":               (2, 15, 12,  1),
    "soccer_china_superleague":                  (3,  1, 11, 30),
    "soccer_saudi_arabia_premier_league":        (9,  1,  5, 31),
    "soccer_australia_aleague":                  (10, 1,  5, 31),
    "soccer_africa_cup_of_nations":              (1,  1,  2, 28),  # Jan–Feb (biennial)
    # ── Soccer — International / Cups ────────────────────────────────────────
    "soccer_fifa_world_cup":                     (6,  1, 12, 31),  # 2026 edition
    "soccer_fifa_club_world_cup":                (6,  1,  7, 20),  # Jun–Jul 2026
    "soccer_conmebol_copa_america":              (6,  1,  7, 31),
    "soccer_uefa_champs_league":                 (9,  1,  6,  1),
    "soccer_uefa_europa_league":                 (9,  1,  5, 31),
    "soccer_uefa_europa_conference_league":      (9,  1,  5, 31),
    "soccer_uefa_nations_league":                (9,  1,  6, 30),
    "soccer_fifa_womens_world_cup":              (7,  1,  9, 15),  # Jul–Aug (quadrennial)
    # ── Combat Sports — year-round ────────────────────────────────────────────
    "mma_mixed_martial_arts":                    (1,  1, 12, 31),
    "boxing_boxing":                             (1,  1, 12, 31),
    # ── Tennis — Grand Slams ──────────────────────────────────────────────────
    "tennis_atp_australian_open":                (1,  1,  2,  5),
    "tennis_wta_aus_open_singles":               (1,  1,  2,  5),
    "tennis_atp_french_open":                    (5, 20,  6, 10),
    "tennis_wta_french_open":                    (5, 20,  6, 10),
    "tennis_atp_wimbledon":                      (6, 25,  7, 15),
    "tennis_wta_wimbledon":                      (6, 25,  7, 15),
    "tennis_atp_us_open":                        (8, 25,  9, 10),
    "tennis_wta_us_open":                        (8, 25,  9, 10),
    # ── Tennis — Tour Events (year-round) ────────────────────────────────────
    "tennis_atp_queens_club_champ":              (6,  1,  6, 25),
    "tennis_atp_halle_open":                     (6,  1,  6, 25),
    "tennis_wta_german_open":                    (6,  1,  6, 25),
    "tennis_atp_toronto":                        (8,  1,  8, 15),
    "tennis_wta_toronto":                        (8,  1,  8, 15),
    "tennis_atp_cincinnati":                     (8, 10,  8, 25),
    "tennis_wta_cincinnati":                     (8, 10,  8, 25),
    "tennis_atp_montreal":                       (8,  1,  8, 15),
    "tennis_wta_montreal":                       (8,  1,  8, 15),
    "tennis_atp_madrid":                         (4, 25,  5, 10),
    "tennis_atp_rome":                           (5, 10,  5, 20),
    "tennis_atp_miami":                          (3, 20,  4,  5),
    "tennis_atp_indian_wells":                   (3,  7,  3, 20),
    # ── Golf ──────────────────────────────────────────────────────────────────
    "golf_pga_tour":                             (1,  1, 12, 31),  # year-round
    "golf_lpga":                                 (1,  1, 12, 31),  # year-round
    "golf_masters_tournament_winner":            (4,  1,  4, 15),
    "golf_pga_championship_winner":              (5,  1,  5, 25),
    "golf_us_open_winner":                       (6,  1,  6, 25),
    "golf_the_open_championship_winner":         (7,  1,  7, 25),
    "golf_dp_world_tour":                        (1,  1, 12, 31),  # European Tour
    # ── Aussie Rules ──────────────────────────────────────────────────────────
    "aussierules_afl":                           (3,  1,  9, 30),
    "aussierules_aflw":                          (8,  1, 12, 15),  # Aug–Dec
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "rugbyleague_nrl":                           (3,  1, 10, 15),
    "rugbyleague_nrl_state_of_origin":           (5,  1,  7, 31),
    "rugbyunion_super_rugby":                    (2,  1,  6, 30),
    "rugbyunion_premiership":                    (9,  1,  6, 30),  # English Premiership
    "rugbyunion_top14":                          (9,  1,  6, 30),  # French Top 14
    "rugbyunion_united_rugby_championship":      (9,  1,  6, 30),
    "rugbyunion_world_cup":                      (9,  1, 11, 15),  # Oct 2027
    # ── Cricket ───────────────────────────────────────────────────────────────
    "cricket_international_t20":                 (1,  1, 12, 31),  # year-round
    "cricket_odi":                               (1,  1, 12, 31),
    "cricket_test_match":                        (1,  1, 12, 31),
    "cricket_ipl":                               (3, 15,  6,  1),  # Mar–Jun
    "cricket_t20_world_cup_womens":              (9,  1, 10, 31),
    "cricket_t20_blast":                         (5,  1,  7, 31),  # England T20 Blast
    # ── Esports ───────────────────────────────────────────────────────────────
    "esports_lol_worlds":                        (10, 1, 11, 15),  # LoL Worlds Oct
    "esports_lol_lck":                           (1,  1,  4, 30),  # LCK Spring
    "esports_csgo_tournaments":                  (1,  1, 12, 31),  # year-round
    # ── Motor Racing ──────────────────────────────────────────────────────────
    "motorsport_formula_1":                      (3,  1, 12,  1),  # Mar – Nov/Dec
    "motorsport_indycar":                        (3,  1,  9, 30),
    "motorsport_nascar_cup_series":              (2,  1, 11, 30),
}

# All sport key aliases (name → api_key) — full master list, season-filtered at runtime
_ALL_SPORTS: dict[str, str] = {
    # ── US Major ──────────────────────────────────────────────────────────────
    "nba":                  "basketball_nba",
    "nfl":                  "americanfootball_nfl",
    "mlb":                  "baseball_mlb",
    "nhl":                  "icehockey_nhl",
    "ncaab":                "basketball_ncaab",
    "ncaaf":                "americanfootball_ncaaf",
    "wnba":                 "basketball_wnba",
    "pwhl":                 "icehockey_pwhl",
    "ncaaw":                "basketball_wncaab",
    "wncaab":               "basketball_wncaab",
    # ── Soccer — US ───────────────────────────────────────────────────────────
    "mls":                  "soccer_usa_mls",
    "nwsl":                 "soccer_usa_nwsl",
    # ── Soccer — Europe Top 5 ─────────────────────────────────────────────────
    "epl":                  "soccer_epl",
    "soccer":               "soccer_epl",
    "laliga":               "soccer_spain_la_liga",
    "bundesliga":           "soccer_germany_bundesliga",
    "seriea":               "soccer_italy_serie_a",
    "ligue1":               "soccer_france_ligue_one",
    # ── Soccer — Europe Other ─────────────────────────────────────────────────
    "eredivisie":           "soccer_netherlands_eredivisie",
    "portugal":             "soccer_portugal_primeira_liga",
    "scotland":             "soccer_spl",
    "turkey":               "soccer_turkey_super_league",
    "belgium":              "soccer_belgium_first_div",
    "greece":               "soccer_greece_super_league",
    "denmark":              "soccer_denmark_superliga",
    "sweden":               "soccer_sweden_allsvenskan",
    "norway":               "soccer_norway_eliteserien",
    "finland":              "soccer_finland_veikkausliiga",
    "austria":              "soccer_austria_bundesliga",
    "switzerland":          "soccer_swiss_superleague",
    "czech":                "soccer_czech_liga",
    "poland":               "soccer_poland_ekstraklasa",
    "romania":              "soccer_romania_liga_1",
    "croatia":              "soccer_croatia_hnl",
    # ── Soccer — Women's Europe ───────────────────────────────────────────────
    "wsl":                  "soccer_england_wsl",
    "wbundesliga":          "soccer_germany_frauen_bundesliga",
    "wspain":               "soccer_spain_liga_f",
    "wfrance":              "soccer_france_d1_feminine",
    "witaly":               "soccer_italy_serie_a_feminine",
    "wuefacl":              "soccer_uefa_womens_champs_league",
    # ── Soccer — South America / Mexico ──────────────────────────────────────
    "brazil":               "soccer_brazil_campeonato",
    "argentina":            "soccer_argentina_primera_division",
    "ligamx":               "soccer_mexico_ligamx",
    "chile":                "soccer_chile_primera_division",
    "colombia":             "soccer_colombia_primera_a",
    "ecuador":              "soccer_ecuador_liga_pro",
    "uruguay":              "soccer_uruguay_primera_division",
    "peru":                 "soccer_peru_primera_division",
    "venezuela":            "soccer_venezuela_primera_liga",
    "conmebol":             "soccer_conmebol_copa_libertadores",
    "copalibertadores":     "soccer_conmebol_copa_libertadores",
    "copasudamericana":     "soccer_conmebol_copa_sudamericana",
    # ── Soccer — Asia / Oceania / Middle East ────────────────────────────────
    "japan":                "soccer_japan_j_league",
    "korea":                "soccer_south_korea_kleague1",
    "china":                "soccer_china_superleague",
    "saudi":                "soccer_saudi_arabia_premier_league",
    "aleague":              "soccer_australia_aleague",
    # ── Soccer — International / Cups ────────────────────────────────────────
    "worldcup":             "soccer_fifa_world_cup",
    "wc":                   "soccer_fifa_world_cup",
    "cwc":                  "soccer_fifa_club_world_cup",
    "clubworldcup":         "soccer_fifa_club_world_cup",
    "copaamerica":          "soccer_conmebol_copa_america",
    "afcon":                "soccer_africa_cup_of_nations",
    "ucl":                  "soccer_uefa_champs_league",
    "champions":            "soccer_uefa_champs_league",
    "europa":               "soccer_uefa_europa_league",
    "conferenceleague":     "soccer_uefa_europa_conference_league",
    "uefanations":          "soccer_uefa_nations_league",
    "wwc":                  "soccer_fifa_womens_world_cup",
    # ── Combat ────────────────────────────────────────────────────────────────
    "ufc":                  "mma_mixed_martial_arts",
    "mma":                  "mma_mixed_martial_arts",
    "boxing":               "boxing_boxing",
    # ── Tennis — Grand Slams ──────────────────────────────────────────────────
    "ausopen":              "tennis_atp_australian_open",
    "wausopen":             "tennis_wta_aus_open_singles",
    "frenchopen":           "tennis_atp_french_open",
    "wfrenchopen":          "tennis_wta_french_open",
    "wimbledon":            "tennis_atp_wimbledon",
    "wwimbledon":           "tennis_wta_wimbledon",
    "usopen":               "tennis_atp_us_open",
    "wusopen":              "tennis_wta_us_open",
    # ── Tennis — Tour Events ──────────────────────────────────────────────────
    "tennis":               "tennis_atp_us_open",   # current tournament — update seasonally
    "atp":                  "tennis_atp_us_open",   # current tournament — update seasonally
    "wta":                  "tennis_wta_german_open",
    "queens":               "tennis_atp_queens_club_champ",
    "halle":                "tennis_atp_halle_open",
    "madrid":               "tennis_atp_madrid",
    "rome":                 "tennis_atp_rome",
    "miami":                "tennis_atp_miami",
    "indianwells":          "tennis_atp_indian_wells",
    "toronto":              "tennis_atp_toronto",
    "cincinnati":           "tennis_atp_cincinnati",
    # ── Golf ──────────────────────────────────────────────────────────────────
    "golf":                 "golf_pga_tour",
    "pga":                  "golf_pga_tour",
    "lpga":                 "golf_lpga",
    "europeantour":         "golf_dp_world_tour",
    "masters":              "golf_masters_tournament_winner",
    "pgachamp":             "golf_pga_championship_winner",
    "usopen_golf":          "golf_us_open_winner",
    "theopen":              "golf_the_open_championship_winner",
    # ── Aussie Rules ──────────────────────────────────────────────────────────
    "afl":                  "aussierules_afl",
    "aflw":                 "aussierules_aflw",
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "nrl":                  "rugbyleague_nrl",
    "stateoforigin":        "rugbyleague_nrl_state_of_origin",
    "superrugby":           "rugbyunion_super_rugby",
    "premiership":          "rugbyunion_premiership",
    "top14":                "rugbyunion_top14",
    "urc":                  "rugbyunion_united_rugby_championship",
    "rugbywc":              "rugbyunion_world_cup",
    # ── Cricket ───────────────────────────────────────────────────────────────
    "t20":                  "cricket_international_t20",
    "odi":                  "cricket_odi",
    "cricket":              "cricket_test_match",
    "testcricket":          "cricket_test_match",
    "ipl":                  "cricket_ipl",
    "t20blast":             "cricket_t20_blast",
    "wt20":                 "cricket_t20_world_cup_womens",
    # ── Motor Racing ──────────────────────────────────────────────────────────
    "f1":                   "motorsport_formula_1",
    "formula1":             "motorsport_formula_1",
    "indycar":              "motorsport_indycar",
    "nascar":               "motorsport_nascar_cup_series",
}




def _build_active_sports() -> dict[str, str]:
    """
    Return full alias→api_key map.
    The Odds API scan itself filters to only sports with live events (via /sports endpoint).
    Season calendar is fallback only — Odds API is source of truth.
    """
    return dict(_ALL_SPORTS)


# ── Sports tracked — full list, Odds API filters to in-season at scan time ────
SPORTS = _build_active_sports()

SPORTSBOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet", "espnbet", "hardrock", "pinnacle"]

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

