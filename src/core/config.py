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
    "baseball_npb":                              (3, 25, 10, 31),  # Japan
    "baseball_kbo":                              (3, 25, 11, 10),  # Korea
    "baseball_cpbl":                             (3, 15, 10, 31),  # Chinese Taipei
    "baseball_lmb":                              (4,  1, 10, 15),  # Mexico
    "baseball_winter_leagues":                   (10, 1,  2, 28),
    "basketball_nba":                            (10, 1,  6, 30),  # Oct – Jun
    "basketball_nba_summer_league":              (7,  1,  7, 31),  # NBA Summer League — Jul
    "basketball_euroleague":                     (10, 1,  5, 31),  # Oct – May
    "basketball_eurocup":                        (10, 1,  5, 31),
    "basketball_fiba_world_cup":                 (8,  1,  9, 30),  # quadrennial
    "basketball_nbl":                            (10, 1,  3, 31),  # Australia NBL
    "basketball_bbl":                            (10, 1,  4, 30),  # Germany BBL
    "basketball_lnb":                            (10, 1,  5, 31),  # France Pro A
    "basketball_liga_acb":                       (10, 1,  6, 15),  # Spain ACB
    "basketball_lba":                            (10, 1,  6, 15),  # Italy LBA
    "basketball_bsl":                            (10, 1,  5, 31),  # Turkey BSL
    "basketball_bbl_uk":                         (10, 1,  4, 30),  # UK BBL
    "basketball_russia_vtb":                     (10, 1,  5, 31),  # VTB United League
    "basketball_wnba":                           (5,  1, 10, 15),  # May – Oct
    "americanfootball_nfl":                      (9,  1,  2, 15),  # Sep – Feb
    "americanfootball_ncaaf":                    (8, 25,  1, 15),  # Aug – Jan
    "americanfootball_cfl":                      (6,  1, 11, 25),  # Jun – Nov
    "basketball_ncaab":                          (11, 1,  4, 10),  # Nov – Apr
    "basketball_ncaab_womens":                   (11, 1,  4, 10),  # Nov – Apr
    "icehockey_nhl":                             (10, 1,  6, 30),  # Oct – Jun
    "icehockey_pwhl":                            (1,  1,  5, 31),  # Jan – May
    "icehockey_ahl":                             (10, 1,  6, 30),
    "icehockey_khl":                             (9,  1,  4, 30),  # KHL Russia
    "icehockey_shl":                             (9,  1,  4, 30),  # Sweden
    "icehockey_liiga":                           (9,  1,  4, 30),  # Finland
    "icehockey_del":                             (9,  1,  4, 30),  # Germany
    "icehockey_nla":                             (9,  1,  4, 30),  # Switzerland
    "icehockey_iihf_world_championship":         (5,  1,  5, 25),
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
    "rugbyleague_super_league":                  (2,  1, 10, 15),  # UK Super League
    "rugbyleague_betfred_championship":          (2,  1, 10, 15),
    "rugbyunion_super_rugby":                    (2,  1,  6, 30),
    "rugbyunion_premiership":                    (9,  1,  6, 30),  # English Premiership
    "rugbyunion_top14":                          (9,  1,  6, 30),  # French Top 14
    "rugbyunion_united_rugby_championship":      (9,  1,  6, 30),
    "rugbyunion_world_cup":                      (9,  1, 11, 15),  # Oct 2027
    "rugbyunion_six_nations":                    (2,  1,  3, 20),
    "rugbyunion_autumn_nations":                 (11, 1, 11, 30),
    "rugbyunion_pacific_nations":                (6,  1,  7, 31),
    "rugbyunion_currie_cup":                     (2,  1,  9, 30),  # South Africa
    "rugbyunion_mitre_10_cup":                   (8,  1, 10, 31),  # New Zealand
    "rugbyunion_super_w":                        (2,  1,  4, 30),  # Women's Super Rugby
    # ── Cricket ───────────────────────────────────────────────────────────────
    "cricket_international_t20":                 (1,  1, 12, 31),  # year-round
    "cricket_odi":                               (1,  1, 12, 31),
    "cricket_test_match":                        (1,  1, 12, 31),
    "cricket_ipl":                               (3, 15,  6,  1),  # Mar–Jun
    "cricket_t20_world_cup_womens":              (9,  1, 10, 31),
    "cricket_t20_blast":                         (5,  1,  7, 31),  # England T20 Blast
    "cricket_bbl":                               (12, 1,  2, 15),  # Big Bash League
    "cricket_psl":                               (2,  1,  3, 31),  # Pakistan Super League
    "cricket_cpl":                               (8,  1,  9, 30),  # Caribbean Premier League
    "cricket_sa20":                              (1,  1,  2, 15),  # SA20 South Africa
    "cricket_the_hundred":                       (7,  1,  9,  1),  # The Hundred
    "cricket_vitality_blast":                    (5,  1,  7, 31),
    "cricket_sheffield_shield":                  (10, 1,  3, 31),  # Australia domestic
    "cricket_plunket_shield":                    (10, 1,  3, 31),  # NZ domestic
    # ── Esports ───────────────────────────────────────────────────────────────
    "esports_lol_worlds":                        (10, 1, 11, 15),  # LoL Worlds Oct
    "esports_lol_lck":                           (1,  1,  4, 30),  # LCK Spring
    "esports_csgo_tournaments":                  (1,  1, 12, 31),  # year-round
    # ── Motor Racing ──────────────────────────────────────────────────────────
    "motorsport_formula_1":                      (3,  1, 12,  1),  # Mar – Nov/Dec
    "motorsport_indycar":                        (3,  1,  9, 30),
    "motorsport_nascar_cup_series":              (2,  1, 11, 30),
    "motorsport_motogp":                         (3,  1, 11, 15),
    "motorsport_wrc":                            (1,  1, 11, 30),  # year-round
    "motorsport_wsbk":                           (2,  1, 11, 15),
    "motorsport_dtm":                            (5,  1, 10, 31),
    "motorsport_imsa":                           (1,  1, 12, 31),
    "motorsport_lemans":                         (6,  1,  6, 30),
    "motorsport_formula2":                       (3,  1, 12,  1),
    "motorsport_formula3":                       (3,  1, 12,  1),
    # ── Darts ─────────────────────────────────────────────────────────────────
    "darts_pdc_world_championship":              (12, 15, 1, 10),
    "darts_premier_league":                      (2,  1,  5, 31),
    "darts_world_grand_prix":                    (10, 1, 10, 15),
    "darts_uk_open":                             (3,  1,  3, 15),
    "darts_world_matchplay":                     (7,  1,  7, 31),
    "darts_grand_slam":                          (11, 1, 11, 20),
    "darts_bdo_world_championship":              (12, 27, 1, 15),
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker_world_championship":                (4,  19, 5,  5),
    "snooker_uk_championship":                   (11, 25, 12, 10),
    "snooker_masters":                           (1,  12, 1, 20),
    "snooker_china_open":                        (3,  25, 4,  5),
    "snooker_players_championship":              (2,  20, 3,  2),
    # ── Cycling ───────────────────────────────────────────────────────────────
    "cycling_tour_de_france":                    (7,  1,  7, 25),
    "cycling_giro_d_italia":                     (5,  1,  5, 28),
    "cycling_la_vuelta":                         (8, 15,  9, 10),
    "cycling_paris_roubaix":                     (4,  5,  4, 15),
    "cycling_tour_of_flanders":                  (4,  1,  4,  7),
    "cycling_milan_san_remo":                    (3, 15,  3, 25),
    "cycling_uci_world_tour":                    (1,  1, 12, 31),
}

# All sport key aliases (name → api_key) — full master list, season-filtered at runtime
_ALL_SPORTS: dict[str, str] = {
    # ── US Major ──────────────────────────────────────────────────────────────
    "nba":                  "basketball_nba",
    "nbasummer":            "basketball_nba_summer_league",
    "summerleague":         "basketball_nba_summer_league",
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
    "ireland":              "soccer_ireland_premier_division",
    "wales":                "soccer_wales_premier_league",
    "serbia":               "soccer_serbia_superliga",
    "ukraine":              "soccer_ukraine_premier_league",
    "hungary":              "soccer_hungary_nb_i",
    "slovakia":             "soccer_slovakia_superliga",
    "bulgaria":             "soccer_bulgaria_efbet_liga",
    "northmacedonia":       "soccer_north_macedonia_1_liga",
    "belarus":              "soccer_belarus_premier_league",
    "israel":               "soccer_israel_premier_league",
    "cyprus":               "soccer_cyprus_first_division",
    "luxembourg":           "soccer_luxembourg_bgl_ligue",
    "moldova":              "soccer_moldova_nationala",
    "slovenia":             "soccer_slovenia_1_snl",
    "albania":              "soccer_albania_superliga",
    "latvia":               "soccer_latvia_virsliga",
    "estonia":              "soccer_estonia_meistriliiga",
    "lithuania":            "soccer_lithuania_a_lyga",
    "kazakhstan":           "soccer_kazakhstan_premier_league",
    "azerbaijan":           "soccer_azerbaijan_premier_league",
    "georgiasoccer":        "soccer_georgia_erovnuli_liga",
    "armenia":              "soccer_armenia_premier_league",
    # ── Soccer — Women's Europe ───────────────────────────────────────────────
    "wsl":                  "soccer_england_wsl",
    "wbundesliga":          "soccer_germany_frauen_bundesliga",
    "wspain":               "soccer_spain_liga_f",
    "wfrance":              "soccer_france_d1_feminine",
    "witaly":               "soccer_italy_serie_a_feminine",
    "wuefacl":              "soccer_uefa_womens_champs_league",
    # ── Soccer — South America / Mexico / CONCACAF ───────────────────────────
    "brazil":               "soccer_brazil_campeonato",
    "argentina":            "soccer_argentina_primera_division",
    "ligamx":               "soccer_mexico_ligamx",
    "chile":                "soccer_chile_primera_division",
    "colombia":             "soccer_colombia_primera_a",
    "ecuador":              "soccer_ecuador_liga_pro",
    "uruguay":              "soccer_uruguay_primera_division",
    "peru":                 "soccer_peru_primera_division",
    "venezuela":            "soccer_venezuela_primera_liga",
    "bolivia":              "soccer_bolivia_division_profesional",
    "paraguay":             "soccer_paraguay_division_profesional",
    "costarica":            "soccer_costa_rica_primera_division",
    "honduras":             "soccer_honduras_liga_nacional",
    "guatemala":            "soccer_guatemala_liga_nacional",
    "panama":               "soccer_panama_liga_panamena",
    "elsalvador":           "soccer_el_salvador_primera_division",
    "nicaragua":            "soccer_nicaragua_primera_division",
    "conmebol":             "soccer_conmebol_copa_libertadores",
    "copalibertadores":     "soccer_conmebol_copa_libertadores",
    "copasudamericana":     "soccer_conmebol_copa_sudamericana",
    "usl":                  "soccer_usa_usl_championship",
    "cpl":                  "soccer_canada_premier_league",
    # ── Soccer — Asia / Middle East ───────────────────────────────────────────
    "japan":                "soccer_japan_j_league",
    "korea":                "soccer_south_korea_kleague1",
    "china":                "soccer_china_superleague",
    "saudi":                "soccer_saudi_arabia_premier_league",
    "aleague":              "soccer_australia_aleague",
    "india":                "soccer_india_super_league",
    "thailand":             "soccer_thailand_league_1",
    "vietnam":              "soccer_vietnam_v_league_1",
    "malaysia":             "soccer_malaysia_super_league",
    "indonesia":            "soccer_indonesia_liga_1",
    "philippines":          "soccer_philippines_pfl",
    "uae":                  "soccer_uae_arabian_gulf_league",
    "qatar":                "soccer_qatar_stars_league",
    "kuwait":               "soccer_kuwait_premier_league",
    "iran":                 "soccer_iran_persian_gulf_pro",
    "iraq":                 "soccer_iraq_premier_league",
    "jordan":               "soccer_jordan_pro_league",
    "bahrain":              "soccer_bahrain_premier_league",
    "oman":                 "soccer_oman_professional_league",
    "uzbekistan":           "soccer_uzbekistan_super_league",
    "tajikistan":           "soccer_tajikistan_vysshaya_liga",
    # ── Soccer — Africa ───────────────────────────────────────────────────────
    "egypt":                "soccer_egypt_premier_league",
    "southafrica":          "soccer_south_africa_psl",
    "nigeria":              "soccer_nigeria_premier_league",
    "ghana":                "soccer_ghana_premier_league",
    "kenya":                "soccer_kenya_premier_league",
    "tanzania":             "soccer_tanzania_premier_league",
    "ethiopia":             "soccer_ethiopia_premier_league",
    "senegal":              "soccer_senegal_premier_league",
    "cameroon":             "soccer_cameroon_elite_one",
    "ivorycoast":           "soccer_ivory_coast_mtn_ligue",
    "morocco":              "soccer_morocco_botola_pro",
    "tunisia":              "soccer_tunisia_ligue_1",
    "algeria":              "soccer_algeria_ligue_professionnelle",
    "zambia":               "soccer_zambia_super_league",
    "zimbabwe":             "soccer_zimbabwe_premier_league",
    # ── Soccer — Oceania ──────────────────────────────────────────────────────
    "newzealand":           "soccer_new_zealand_npl",
    "fiji":                 "soccer_fiji_ofc",
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
    "premierleague":        "soccer_epl",
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
    # ── Tennis — Tour Events + team competitions ──────────────────────────────
    "tennis":               "tennis_atp_wimbledon",
    "atp":                  "tennis_atp_wimbledon",
    "wta":                  "tennis_wta_wimbledon",
    "queens":               "tennis_atp_queens_club_champ",
    "halle":                "tennis_atp_halle_open",
    "madrid":               "tennis_atp_madrid",
    "rome":                 "tennis_atp_rome",
    "miami":                "tennis_atp_miami",
    "indianwells":          "tennis_atp_indian_wells",
    "toronto":              "tennis_atp_toronto",
    "montreal":             "tennis_atp_montreal",
    "cincinnati":           "tennis_atp_cincinnati",
    "wtacincinnati":        "tennis_wta_cincinnati",
    "wtatoronto":           "tennis_wta_toronto",
    "challenger":           "tennis_atp_challenger",
    "wtachallenger":        "tennis_wta_challenger",
    "daviscup":             "tennis_davis_cup",
    "bjkc":                 "tennis_billie_jean_king_cup",
    "lavercup":             "tennis_laver_cup",
    "unitedcup":            "tennis_united_cup",
    # ── Golf ──────────────────────────────────────────────────────────────────
    "golf":                 "golf_pga_tour",
    "pga":                  "golf_pga_tour",
    "lpga":                 "golf_lpga",
    "europeantour":         "golf_dp_world_tour",
    "dpworld":              "golf_dp_world_tour",
    "masters":              "golf_masters_tournament_winner",
    "pgachamp":             "golf_pga_championship_winner",
    "usopen_golf":          "golf_us_open_winner",
    "theopen":              "golf_the_open_championship_winner",
    # ── Aussie Rules (Men + Women) ────────────────────────────────────────────
    "afl":                  "aussierules_afl",
    "aflw":                 "aussierules_aflw",
    # ── Rugby — all competitions (Men + Women) ────────────────────────────────
    "nrl":                  "rugbyleague_nrl",
    "stateoforigin":        "rugbyleague_nrl_state_of_origin",
    "superrugby":           "rugbyunion_super_rugby",
    "premiership":          "rugbyunion_premiership",
    "top14":                "rugbyunion_top14",
    "urc":                  "rugbyunion_united_rugby_championship",
    "rugbywc":              "rugbyunion_world_cup",
    "wrugbywc":             "rugbyunion_women_world_cup",
    "sixnations":           "rugbyunion_six_nations",
    "autumnnations":        "rugbyunion_autumn_nations",
    "pacificnations":       "rugbyunion_pacific_nations",
    "curriecup":            "rugbyunion_currie_cup",
    "mitre10":              "rugbyunion_mitre_10_cup",
    "superw":               "rugbyunion_super_w",
    "superleague":          "rugbyleague_super_league",
    "betfredchamp":         "rugbyleague_betfred_championship",
    # ── Cricket — all formats + franchise leagues ─────────────────────────────
    "t20":                  "cricket_international_t20",
    "odi":                  "cricket_odi",
    "cricket":              "cricket_test_match",
    "testcricket":          "cricket_test_match",
    "ipl":                  "cricket_ipl",
    "t20blast":             "cricket_t20_blast",
    "wt20":                 "cricket_t20_world_cup_womens",
    "bblcricket":           "cricket_bbl",
    "psl":                  "cricket_psl",
    "caribbeancpl":         "cricket_cpl",
    "sa20":                 "cricket_sa20",
    "thehundred":           "cricket_the_hundred",
    "vitalityblast":        "cricket_vitality_blast",
    # ── Motor Racing ──────────────────────────────────────────────────────────
    "f1":                   "motorsport_formula_1",
    "formula1":             "motorsport_formula_1",
    "indycar":              "motorsport_indycar",
    "nascar":               "motorsport_nascar_cup_series",
    "motogp":               "motorsport_motogp",
    "wrc":                  "motorsport_wrc",
    "wsbk":                 "motorsport_wsbk",
    "dtm":                  "motorsport_dtm",
    "imsa":                 "motorsport_imsa",
    "lemans":               "motorsport_lemans",
    "formula2":             "motorsport_formula2",
    "f2":                   "motorsport_formula2",
    "formula3":             "motorsport_formula3",
    "f3":                   "motorsport_formula3",
    # ── Basketball — International ────────────────────────────────────────────
    "euroleague":           "basketball_euroleague",
    "eurocup":              "basketball_eurocup",
    "fiba":                 "basketball_fiba_world_cup",
    "fibawc":               "basketball_fiba_world_cup",
    "nbl":                  "basketball_nbl",
    "acb":                  "basketball_liga_acb",
    "lba":                  "basketball_lba",
    "turkishbsl":           "basketball_bsl",
    "vtb":                  "basketball_russia_vtb",
    # ── Ice Hockey — International ────────────────────────────────────────────
    "ahl":                  "icehockey_ahl",
    "khl":                  "icehockey_khl",
    "shl":                  "icehockey_shl",
    "liiga":                "icehockey_liiga",
    "del":                  "icehockey_del",
    "nla":                  "icehockey_nla",
    "iihf":                 "icehockey_iihf_world_championship",
    # ── Baseball — International ──────────────────────────────────────────────
    "npb":                  "baseball_npb",
    "kbo":                  "baseball_kbo",
    "cpbl":                 "baseball_cpbl",
    "lmb":                  "baseball_lmb",
    "winterleagues":        "baseball_winter_leagues",
    # ── American / Canadian Football ──────────────────────────────────────────
    "cfl":                  "americanfootball_cfl",
    # ── Darts ────────────────────────────────────────────────────────────────
    "darts":                "darts_pdc_world_championship",
    "pdc":                  "darts_pdc_world_championship",
    "dartspremier":         "darts_premier_league",
    "grandprix":            "darts_world_grand_prix",
    "ukopen":               "darts_uk_open",
    "worldmatchplay":       "darts_world_matchplay",
    "grandslam":            "darts_grand_slam",
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker":              "snooker_world_championship",
    "ukchampionship":       "snooker_uk_championship",
    "snookermasters":       "snooker_masters",
    "chinaopen":            "snooker_china_open",
    # ── Cycling ───────────────────────────────────────────────────────────────
    "tdf":                  "cycling_tour_de_france",
    "tourdefrance":         "cycling_tour_de_france",
    "giro":                 "cycling_giro_d_italia",
    "vuelta":               "cycling_la_vuelta",
    "cycling":              "cycling_uci_world_tour",
    "parisroubaix":         "cycling_paris_roubaix",
    "flanders":             "cycling_tour_of_flanders",
    "milanremo":            "cycling_milan_san_remo",
    # ── Volleyball ────────────────────────────────────────────────────────────
    "volleyball":           "volleyball_wvl",
    "wvl":                  "volleyball_wvl",
    "vbmenswc":             "volleyball_men_wc",
    "vbwomenswc":           "volleyball_women_wc",
    "cevcl":                "volleyball_cev_champions_league",
    "ncaav":                "volleyball_ncaav",
    # ── Handball ─────────────────────────────────────────────────────────────
    "handball":             "handball_world_championship",
    "ehfcl":                "handball_ehf_champions_league",
    "handballbundesliga":   "handball_bundesliga",
    # ── Table Tennis ─────────────────────────────────────────────────────────
    "tabletennis":          "tabletennis_wtt",
    "wtt":                  "tabletennis_wtt",
    "ittf":                 "tabletennis_ittf",
    # ── Badminton ────────────────────────────────────────────────────────────
    "badminton":            "badminton_bwf_world_tour",
    "bwf":                  "badminton_bwf_world_tour",
    # ── Esports ──────────────────────────────────────────────────────────────
    "lol":                  "esports_lol",
    "csgo":                 "esports_csgo",
    "cs2":                  "esports_csgo",
    "dota2":                "esports_dota2",
    # ── Beach Volleyball ─────────────────────────────────────────────────────
    "beachvolleyball":      "beachvolleyball_fivb_pro_tour",
    # ── Field Hockey ─────────────────────────────────────────────────────────
    "fieldhockey":          "fieldhockey_fih_pro_league",
    "fieldhockeywc":        "fieldhockey_world_cup",
    # ── Water Polo ───────────────────────────────────────────────────────────
    "waterpolo":            "waterpolo_len_champions_league",
    # ── Futsal ───────────────────────────────────────────────────────────────
    "futsal":               "futsal_fifa_world_cup",
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

