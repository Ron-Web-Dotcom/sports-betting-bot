"""
SofaScore adapter.

SofaScore provides real-time scores, form tables, head-to-head records,
player statistics, standings, and match event timelines across virtually
every sport (soccer, basketball, NFL, NHL, MLB, tennis, MMA, esports, etc.).

Uses SofaScore's unofficial public API — no key required.
Endpoint base: https://api.sofascore.com/api/v1

Key endpoints used:
  GET /sport/{sport}/events/live                 live matches
  GET /sport/{sport}/scheduled-events/{date}     matches on a given date (YYYY-MM-DD)
  GET /event/{id}/statistics                     full match stats
  GET /event/{id}/h2h/events                     head-to-head history
  GET /event/{id}/form                           recent form for both teams
  GET /team/{id}/events/last/0                   last 5 team matches
  GET /team/{id}/standings/seasons               standings
  GET /player/{id}/statistics/season/{season_id} season stats
  GET /team/{id}/players                         squad list with positions
"""
import logging
import threading
import time
from datetime import datetime
from urllib.parse import quote
from src.core.timezone import et_naive, ET

logger = logging.getLogger(__name__)

_BASE = "https://api.sofascore.com/api/v1"

# Sentinel returned by _generic_proxy_get when the request succeeded at the proxy
# level but the sport/date has no data (404 or 422). Callers treat it like None
# (no data) but _get() does NOT count it as a circuit-breaker failure.
_NO_DATA = object()

# ── Circuit breaker — trip after 3 consecutive 403s, reset after 30 min ────────
_cb_lock         = threading.Lock()
_cb_failures     = 0
_cb_tripped_at   = 0.0
_CB_THRESHOLD    = 10     # trip after 10 consecutive 403s — transient blocks shouldn't trip this
_CB_RESET_SECS   = 120    # try again after 2 minutes — Decodo recovers fast

def _cb_is_open() -> bool:
    """Return True if circuit is tripped (Sofascore is blocked)."""
    global _cb_failures
    with _cb_lock:
        if _cb_failures >= _CB_THRESHOLD:
            if time.monotonic() - _cb_tripped_at < _CB_RESET_SECS:
                return True
            # Cooldown elapsed — reset counter so probe needs N failures to re-trip
            _cb_failures = 0
            return False
        return False

def _cb_record_failure():
    global _cb_failures, _cb_tripped_at
    with _cb_lock:
        _cb_failures += 1
        if _cb_failures >= _CB_THRESHOLD:
            _cb_tripped_at = time.monotonic()
            logger.warning("Sofascore circuit breaker TRIPPED — skipping all calls for %ds", _CB_RESET_SECS)

def _cb_record_success():
    global _cb_failures
    with _cb_lock:
        _cb_failures = 0

import os as _os

# ── Complete list of ALL Sofascore sport slugs ───────────────────────────────
# Used by get_all_scheduled_events to scan EVERY sport worldwide — including sports
# that have no Odds API equivalent (darts, cycling, futsal, beach volleyball, etc.)
# Source: Sofascore's /sport/ endpoint catalogue.
ALL_SF_SLUGS: list[str] = [
    # ── Ball sports ───────────────────────────────────────────────────────────
    "football",               # Soccer / association football (worldwide — largest category)
    "basketball",             # NBA, EuroLeague, NBL, FIBA, WNBA, NCAAB, etc.
    "american-football",      # NFL, NCAAF, CFL, Arena Football
    "baseball",               # MLB, NPB (Japan), KBO (Korea), LMB (Mexico)
    "ice-hockey",             # NHL, KHL, SHL, DEL, PWHL, IIHF
    "handball",               # EHF Champions League, Bundesliga, Liga ASOBAL, IHF
    "volleyball",             # FIVB, CEV Champions League, Serie A, Superliga
    "beach-volleyball",       # FIVB Beach Pro Tour
    "futsal",                 # FIFA Futsal World Cup, UEFA Futsal, Brazilian Futsal
    "water-polo",             # LEN Champions League, World League, national leagues
    "field-hockey",           # FIH Pro League, Hockey Champions Trophy, Bundesliga
    "floorball",              # World Championship, Finnish and Swedish leagues
    "bandy",                  # World Championship, Elitserien (Sweden), Superleague (Russia)
    # ── Racket sports ─────────────────────────────────────────────────────────
    "tennis",                 # ATP Tour, WTA Tour, Grand Slams, Davis Cup, BJK Cup
    "table-tennis",           # WTT, ITTF, Bundesliga, Premier League
    "badminton",              # BWF World Tour, Super Series, national leagues
    "squash",                 # PSA World Tour, British Open, World Championship
    # ── Combat sports ─────────────────────────────────────────────────────────
    "mma",                    # UFC, Bellator, ONE Championship, PFL
    "boxing",                 # All major sanctioning bodies worldwide
    "sumo",                   # Japanese sumo tournaments
    # ── Motor sports ─────────────────────────────────────────────────────────
    "formula-1",              # F1 World Championship
    "motorsport",             # MotoGP, Indycar, NASCAR, WRC, WSBK, DTM
    # ── Golf ─────────────────────────────────────────────────────────────────
    "golf",                   # PGA Tour, DP World Tour, LPGA, LIV, Majors
    # ── Outdoor / track & field ───────────────────────────────────────────────
    "cycling",                # Tour de France, Giro, Vuelta, Classics, UCI WorldTour
    "athletics",              # Diamond League, World Championships, Olympic events
    # ── Bat & ball ────────────────────────────────────────────────────────────
    "cricket",                # Test, ODI, T20I, IPL, BBL, PSL, CPL, SA20
    # ── Oval-ball sports ──────────────────────────────────────────────────────
    "rugby",                  # Rugby Union: RWC, URC, Premiership, Super Rugby, Top 14
    "rugby-league",           # NRL, Super League, State of Origin, RLWC
    # ── Antipodean ────────────────────────────────────────────────────────────
    "australian-football",    # AFL, AFLW
    # ── Cue sports ────────────────────────────────────────────────────────────
    "snooker",                # World Championship, UK Championship, Masters
    "darts",                  # PDC World Championship, Premier League, World Series
    # ── Snow & ice ────────────────────────────────────────────────────────────
    "ski-jumping",            # FIS Ski Jumping World Cup
    "biathlon",               # IBU World Cup, World Championships
    "cross-country",          # FIS Cross-Country World Cup
    # ── Esports ───────────────────────────────────────────────────────────────
    "esports",                # LoL, CS2/CSGO, Dota 2, Valorant, Overwatch, Rocket League
]

# Maps our internal Odds API sport_key → SofaScore sport slug
SPORT_MAP = {
    # ── US Sports ─────────────────────────────────────────────────────────────
    "americanfootball_nfl":                  "american-football",
    "americanfootball_ncaaf":                "american-football",
    "basketball_nba":                        "basketball",
    "basketball_wnba":                       "basketball",
    "basketball_ncaab":                      "basketball",
    "basketball_wncaab":                     "basketball",
    "baseball_mlb":                          "baseball",
    "icehockey_nhl":                         "ice-hockey",
    # ── Soccer — Top Leagues ──────────────────────────────────────────────────
    "soccer_epl":                            "football",
    "soccer_spain_la_liga":                  "football",
    "soccer_germany_bundesliga":             "football",
    "soccer_italy_serie_a":                  "football",
    "soccer_france_ligue_one":               "football",
    "soccer_usa_mls":                        "football",
    "soccer_netherlands_eredivisie":         "football",
    "soccer_portugal_primeira_liga":         "football",
    "soccer_mexico_ligamx":                  "football",
    "soccer_argentina_primera_division":     "football",
    "soccer_brazil_campeonato":              "football",
    "soccer_turkey_super_league":            "football",
    "soccer_spl":                            "football",   # Scottish Premiership
    # ── Soccer — Cups & International ────────────────────────────────────────
    "soccer_uefa_champs_league":             "football",
    "soccer_uefa_europa_league":             "football",
    "soccer_uefa_europa_conference_league":  "football",
    "soccer_fifa_world_cup":                 "football",
    "soccer_fifa_club_world_cup":            "football",   # Club World Cup 2026
    "soccer_conmebol_copa_america":          "football",
    "soccer_conmebol_copa_libertadores":     "football",
    "soccer_conmebol_copa_sudamericana":     "football",   # Copa Sudamericana
    "soccer_africa_cup_of_nations":          "football",
    "soccer_uefa_nations_league":            "football",
    # ── Soccer — Europe Other ─────────────────────────────────────────────────
    "soccer_belgium_first_div":              "football",
    "soccer_greece_super_league":            "football",
    "soccer_denmark_superliga":              "football",
    "soccer_sweden_allsvenskan":             "football",
    "soccer_norway_eliteserien":             "football",
    "soccer_finland_veikkausliiga":          "football",
    "soccer_austria_bundesliga":             "football",
    "soccer_swiss_superleague":              "football",
    "soccer_czech_liga":                     "football",
    "soccer_poland_ekstraklasa":             "football",
    "soccer_romania_liga_1":                 "football",
    "soccer_croatia_hnl":                    "football",
    # ── Soccer — South America ────────────────────────────────────────────────
    "soccer_chile_primera_division":         "football",
    "soccer_colombia_primera_a":             "football",
    "soccer_ecuador_liga_pro":               "football",
    "soccer_uruguay_primera_division":       "football",
    "soccer_peru_primera_division":          "football",
    "soccer_venezuela_primera_liga":         "football",
    # ── Soccer — Asia / Oceania / Middle East ─────────────────────────────────
    "soccer_japan_j_league":                 "football",
    "soccer_south_korea_kleague1":           "football",
    "soccer_china_superleague":              "football",
    "soccer_saudi_arabia_premier_league":    "football",
    "soccer_australia_aleague":              "football",
    # ── Women's Soccer ────────────────────────────────────────────────────────
    "soccer_usa_nwsl":                       "football",
    "soccer_fifa_womens_world_cup":          "football",
    "soccer_uefa_womens_champs_league":      "football",
    "soccer_england_wsl":                    "football",
    "soccer_germany_frauen_bundesliga":      "football",
    "soccer_spain_liga_f":                   "football",
    "soccer_france_d1_feminine":             "football",
    "soccer_italy_serie_a_feminine":         "football",
    # ── Golf ─────────────────────────────────────────────────────────────────
    "golf_pga_tour":                         "golf",
    "golf_masters_tournament_winner":        "golf",
    "golf_pga_championship_winner":          "golf",
    "golf_us_open_winner":                   "golf",
    "golf_the_open_championship_winner":     "golf",
    "golf_lpga":                             "golf",
    "golf_dp_world_tour":                    "golf",
    # ── Motorsport ───────────────────────────────────────────────────────────
    "motorsport_formula_1":                  "formula-1",
    "motorsport_indycar":                    "motorsport",
    "motorsport_nascar_cup_series":          "motorsport",
    # ── Combat Sports ────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":                "mma",
    "boxing_boxing":                         "boxing",
    # ── Tennis — Grand Slams + Active Tour Events ─────────────────────────────
    "tennis_atp_french_open":                "tennis",
    "tennis_wta_french_open":                "tennis",
    "tennis_atp_wimbledon":                  "tennis",
    "tennis_wta_wimbledon":                  "tennis",
    "tennis_atp_us_open":                    "tennis",
    "tennis_wta_us_open":                    "tennis",
    "tennis_atp_australian_open":            "tennis",
    "tennis_wta_aus_open_singles":           "tennis",
    "tennis_atp_queens_club_champ":          "tennis",
    "tennis_atp_halle_open":                 "tennis",
    "tennis_wta_german_open":                "tennis",
    "tennis_atp_madrid":                     "tennis",
    "tennis_atp_rome":                       "tennis",
    "tennis_atp_miami":                      "tennis",
    "tennis_atp_indian_wells":               "tennis",
    "tennis_atp_toronto":                    "tennis",
    "tennis_atp_cincinnati":                 "tennis",
    # ── Women's Hockey ────────────────────────────────────────────────────────
    "icehockey_pwhl":                        "ice-hockey",
    # ── Aussie Rules (Men's + Women's) ───────────────────────────────────────
    "aussierules_afl":                       "australian-football",
    "aussierules_aflw":                      "australian-football",
    # ── Rugby (Men's + Women's) ───────────────────────────────────────────────
    "rugbyunion_world_cup":                  "rugby",
    "rugbyunion_women_world_cup":            "rugby",
    "rugbyunion_super_rugby":                "rugby",
    "rugbyunion_premiership":                "rugby",
    "rugbyunion_top14":                      "rugby",
    "rugbyunion_united_rugby_championship":  "rugby",
    "rugbyleague_nrl":                       "rugby-league",
    "rugbyleague_nrl_state_of_origin":       "rugby-league",
    # ── Cricket (Men's + Women's) ─────────────────────────────────────────────
    "cricket_icc_world_cup":                 "cricket",
    "cricket_ipl":                           "cricket",
    "cricket_icc_womens_t20_wc":             "cricket",
    "cricket_test_match":                    "cricket",
    "cricket_odi":                           "cricket",
    "cricket_international_t20":             "cricket",
    "cricket_t20_world_cup_womens":          "cricket",
    "cricket_t20_blast":                     "cricket",
    # ── Table Tennis ──────────────────────────────────────────────────────────
    "tabletennis_wtt":                       "table-tennis",
    "tabletennis_ittf":                      "table-tennis",
    # ── Volleyball ────────────────────────────────────────────────────────────
    "volleyball_wvl":                        "volleyball",
    "volleyball_men_wc":                     "volleyball",
    "volleyball_women_wc":                   "volleyball",
    # ── Handball ──────────────────────────────────────────────────────────────
    "handball_world_championship":           "handball",
    "handball_ehf_champions_league":         "handball",
    # ── Badminton ─────────────────────────────────────────────────────────────
    "badminton_bwf_world_tour":              "badminton",
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker_world_championship":            "snooker",
    # ── Esports ───────────────────────────────────────────────────────────────
    "esports_lol":                           "esports",
    "esports_csgo":                          "esports",
    "esports_dota2":                         "esports",
    # ── Basketball — International / European ────────────────────────────────
    "basketball_euroleague":                 "basketball",
    "basketball_eurocup":                    "basketball",
    "basketball_fiba_world_cup":             "basketball",
    "basketball_nbl":                        "basketball",   # Australia NBL
    "basketball_bbl":                        "basketball",   # Germany BBL
    "basketball_lnb":                        "basketball",   # France Pro A
    "basketball_liga_acb":                   "basketball",   # Spain ACB
    "basketball_lba":                        "basketball",   # Italy LBA
    "basketball_bsl":                        "basketball",   # Turkey BSL
    "basketball_bbl_uk":                     "basketball",   # UK BBL
    "basketball_russia_vtb":                 "basketball",   # VTB United League
    # ── Ice Hockey — International ───────────────────────────────────────────
    "icehockey_khl":                         "ice-hockey",
    "icehockey_shl":                         "ice-hockey",   # Sweden
    "icehockey_liiga":                       "ice-hockey",   # Finland
    "icehockey_del":                         "ice-hockey",   # Germany
    "icehockey_nla":                         "ice-hockey",   # Switzerland NL
    "icehockey_nl_a":                        "ice-hockey",   # Swiss NL
    "icehockey_ahl":                         "ice-hockey",   # AHL (North America)
    "icehockey_iihf_world_championship":     "ice-hockey",
    # ── Baseball — International ─────────────────────────────────────────────
    "baseball_npb":                          "baseball",     # Japan NPB
    "baseball_kbo":                          "baseball",     # Korea KBO
    "baseball_cpbl":                         "baseball",     # Chinese Taipei CPBL
    "baseball_lmb":                          "baseball",     # Mexico LMB
    "baseball_winter_leagues":               "baseball",
    # ── American Football ────────────────────────────────────────────────────
    "americanfootball_cfl":                  "american-football",  # CFL Canada
    # ── Soccer — More European Leagues ───────────────────────────────────────
    "soccer_ireland_premier_division":       "football",
    "soccer_wales_premier_league":           "football",
    "soccer_serbia_superliga":               "football",
    "soccer_ukraine_premier_league":         "football",
    "soccer_hungary_nb_i":                   "football",
    "soccer_slovakia_superliga":             "football",
    "soccer_bulgaria_efbet_liga":            "football",
    "soccer_north_macedonia_1_liga":         "football",
    "soccer_belarus_premier_league":         "football",
    "soccer_israel_premier_league":          "football",
    "soccer_cyprus_first_division":          "football",
    "soccer_luxembourg_bgl_ligue":           "football",
    "soccer_liechtenstein_cup":              "football",
    "soccer_moldova_nationala":              "football",
    "soccer_slovenia_1_snl":                 "football",
    "soccer_albania_superliga":              "football",
    "soccer_latvia_virsliga":                "football",
    "soccer_estonia_meistriliiga":           "football",
    "soccer_lithuania_a_lyga":               "football",
    "soccer_kazakhstan_premier_league":      "football",
    "soccer_azerbaijan_premier_league":      "football",
    "soccer_georgia_erovnuli_liga":          "football",
    "soccer_armenia_premier_league":         "football",
    # ── Soccer — Asia (Extended) ──────────────────────────────────────────────
    "soccer_india_super_league":             "football",    # ISL India
    "soccer_thailand_league_1":              "football",
    "soccer_vietnam_v_league_1":             "football",
    "soccer_malaysia_super_league":          "football",
    "soccer_indonesia_liga_1":               "football",
    "soccer_philippines_pfl":                "football",
    "soccer_uae_arabian_gulf_league":        "football",
    "soccer_qatar_stars_league":             "football",
    "soccer_kuwait_premier_league":          "football",
    "soccer_iran_persian_gulf_pro":          "football",
    "soccer_iraq_premier_league":            "football",
    "soccer_jordan_pro_league":              "football",
    "soccer_bahrain_premier_league":         "football",
    "soccer_oman_professional_league":       "football",
    "soccer_uzbekistan_super_league":        "football",
    "soccer_tajikistan_vysshaya_liga":       "football",
    # ── Soccer — Africa ───────────────────────────────────────────────────────
    "soccer_egypt_premier_league":           "football",
    "soccer_south_africa_psl":               "football",
    "soccer_nigeria_premier_league":         "football",
    "soccer_ghana_premier_league":           "football",
    "soccer_kenya_premier_league":           "football",
    "soccer_tanzania_premier_league":        "football",
    "soccer_ethiopia_premier_league":        "football",
    "soccer_senegal_premier_league":         "football",
    "soccer_cameroon_elite_one":             "football",
    "soccer_ivory_coast_mtn_ligue":          "football",
    "soccer_morocco_botola_pro":             "football",
    "soccer_tunisia_ligue_1":                "football",
    "soccer_algeria_ligue_professionnelle":  "football",
    "soccer_libya_premier_league":           "football",
    "soccer_zambia_super_league":            "football",
    "soccer_zimbabwe_premier_league":        "football",
    # ── Soccer — Americas (Extended) ─────────────────────────────────────────
    "soccer_usa_usl_championship":           "football",    # USL Championship
    "soccer_canada_premier_league":          "football",
    "soccer_costa_rica_primera_division":    "football",
    "soccer_honduras_liga_nacional":         "football",
    "soccer_guatemala_liga_nacional":        "football",
    "soccer_panama_liga_panamena":           "football",
    "soccer_el_salvador_primera_division":   "football",
    "soccer_nicaragua_primera_division":     "football",
    "soccer_bolivia_division_profesional":   "football",
    "soccer_paraguay_division_profesional":  "football",
    # ── Soccer — Oceania ─────────────────────────────────────────────────────
    "soccer_new_zealand_npl":                "football",
    "soccer_fiji_ofc":                       "football",
    # ── Tennis — Full ATP/WTA Calendar ───────────────────────────────────────
    "tennis_atp_challenger":                 "tennis",
    "tennis_wta_challenger":                 "tennis",
    "tennis_davis_cup":                      "tennis",
    "tennis_billie_jean_king_cup":           "tennis",
    "tennis_laver_cup":                      "tennis",
    "tennis_united_cup":                     "tennis",
    # ── Handball ─────────────────────────────────────────────────────────────
    "handball_bundesliga":                   "handball",
    "handball_starligue":                    "handball",    # France
    "handball_liga_asobal":                  "handball",    # Spain
    "handball_seha_league":                  "handball",
    "handball_shl":                          "handball",    # Sweden
    "handball_nbl":                          "handball",    # Denmark
    "handball_pbp_ekstraklasa":              "handball",    # Poland
    # ── Volleyball ────────────────────────────────────────────────────────────
    "volleyball_cev_champions_league":       "volleyball",
    "volleyball_superliga_men":              "volleyball",  # Russia/international
    "volleyball_serie_a1":                   "volleyball",  # Italy
    "volleyball_plusliga":                   "volleyball",  # Poland
    "volleyball_bundesliga_vbl":             "volleyball",  # Germany
    "volleyball_liga_max":                   "volleyball",  # Turkey
    "volleyball_ncaav":                      "volleyball",  # NCAA volleyball
    # ── Darts ─────────────────────────────────────────────────────────────────
    "darts_pdc_world_championship":          "darts",
    "darts_premier_league":                  "darts",
    "darts_world_grand_prix":                "darts",
    "darts_uk_open":                         "darts",
    "darts_world_matchplay":                 "darts",
    "darts_grand_slam":                      "darts",
    "darts_bdo_world_championship":          "darts",
    # ── Snooker (Extended) ────────────────────────────────────────────────────
    "snooker_uk_championship":               "snooker",
    "snooker_masters":                       "snooker",
    "snooker_china_open":                    "snooker",
    "snooker_players_championship":          "snooker",
    # ── Cycling ───────────────────────────────────────────────────────────────
    "cycling_tour_de_france":                "cycling",
    "cycling_giro_d_italia":                 "cycling",
    "cycling_la_vuelta":                     "cycling",
    "cycling_paris_roubaix":                 "cycling",
    "cycling_tour_of_flanders":              "cycling",
    "cycling_milan_san_remo":                "cycling",
    "cycling_uci_world_tour":                "cycling",
    # ── Rugby Union (Extended) ────────────────────────────────────────────────
    "rugbyunion_six_nations":                "rugby",
    "rugbyunion_autumn_nations":             "rugby",
    "rugbyunion_pacific_nations":            "rugby",
    "rugbyunion_currie_cup":                 "rugby",       # South Africa
    "rugbyunion_mitre_10_cup":               "rugby",       # New Zealand
    "rugbyunion_super_w":                    "rugby",       # Women's Super Rugby
    # ── Rugby League (Extended) ───────────────────────────────────────────────
    "rugbyleague_super_league":              "rugby-league",
    "rugbyleague_betfred_championship":      "rugby-league",
    # ── Cricket (Extended) ────────────────────────────────────────────────────
    "cricket_bbl":                           "cricket",     # Big Bash League
    "cricket_psl":                           "cricket",     # Pakistan Super League
    "cricket_cpl":                           "cricket",     # Caribbean Premier League
    "cricket_sa20":                          "cricket",     # SA20 South Africa
    "cricket_the_hundred":                   "cricket",     # The Hundred (England)
    "cricket_vitality_blast":                "cricket",     # Vitality T20 Blast
    "cricket_sheffield_shield":              "cricket",     # Sheffield Shield (Australia)
    "cricket_plunket_shield":                "cricket",     # Plunket Shield (NZ)
    # ── Beach Volleyball ─────────────────────────────────────────────────────
    "beachvolleyball_fivb_pro_tour":         "beach-volleyball",
    "beachvolleyball_world_championship":    "beach-volleyball",
    # ── Futsal ────────────────────────────────────────────────────────────────
    "futsal_fifa_world_cup":                 "futsal",
    "futsal_uefa_futsal_euro":               "futsal",
    "futsal_liga_nacional":                  "futsal",      # Spain
    "futsal_liga_futsal":                    "futsal",      # Brazil
    # ── Table Tennis (Extended) ───────────────────────────────────────────────
    "tabletennis_bundesliga":                "table-tennis",
    "tabletennis_ttsl":                      "table-tennis", # Table Tennis Super League (China)
    # ── Badminton (Extended) ──────────────────────────────────────────────────
    "badminton_all_england":                 "badminton",
    "badminton_thomas_cup":                  "badminton",
    "badminton_uber_cup":                    "badminton",
    "badminton_sudirman_cup":                "badminton",
    # ── Motorsport (Extended) ────────────────────────────────────────────────
    "motorsport_motogp":                     "motorsport",
    "motorsport_wrc":                        "motorsport",   # World Rally Championship
    "motorsport_wsbk":                       "motorsport",   # World Superbike
    "motorsport_dtm":                        "motorsport",
    "motorsport_imsa":                       "motorsport",
    "motorsport_lemans":                     "motorsport",
    "motorsport_formula2":                   "motorsport",
    "motorsport_formula3":                   "motorsport",
    # ── Winter sports ────────────────────────────────────────────────────────
    "skiing_alpine":                         "ski-jumping",  # placeholder slug
    "biathlon_world_cup":                    "biathlon",
    "crosscountry_world_cup":                "cross-country",
    # ── Floorball ────────────────────────────────────────────────────────────
    "floorball_world_championship":          "floorball",
    "floorball_ssl":                         "floorball",    # Swedish SSL
    "floorball_f_liiga":                     "floorball",    # Finnish F-liiga
    # ── Water polo ────────────────────────────────────────────────────────────
    "waterpolo_len_champions_league":        "water-polo",
    "waterpolo_world_league":                "water-polo",
    # ── Field hockey ─────────────────────────────────────────────────────────
    "fieldhockey_fih_pro_league":            "field-hockey",
    "fieldhockey_world_cup":                 "field-hockey",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept":              "application/json, text/plain, */*",
    "Accept-Language":     "en-US,en;q=0.9",
    "Accept-Encoding":     "gzip, deflate, br",
    "Referer":             "https://www.sofascore.com/",
    "Origin":              "https://www.sofascore.com",
    "Cache-Control":       "no-cache",
    "Pragma":              "no-cache",
    "sec-ch-ua":           '"Google Chrome";v="137", "Chromium";v="137", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile":    "?0",
    "sec-ch-ua-platform":  '"Windows"',
    "Sec-Fetch-Dest":      "empty",
    "Sec-Fetch-Mode":      "cors",
    "Sec-Fetch-Site":      "same-origin",
    # Session cookie — set by sofascore.com on first visit; helps bypass bot detection.
    # Override via SOFASCORE_SESSION env var if the default gets blocked.
    "Cookie":              __import__('os').getenv("SOFASCORE_SESSION", "TS_session=1; sofascore_gdpr_consent=1"),
}


def _slug(sport_key: str) -> str | None:
    return SPORT_MAP.get(sport_key)



def _generic_proxy_get(path: str, proxy_url: str) -> dict | list | None:
    """
    Make a Sofascore request via ZenRows API mode or standard HTTP proxy.

    ZenRows API mode (preferred — more reliable than proxy mode):
      SOFASCORE_PROXY_URL=https://api.zenrows.com/v1/?apikey=KEY
      → appends &url=TARGET to each request

    Standard proxy mode (fallback):
      SOFASCORE_PROXY_URL=http://user:pass@proxy.host:port
      → routes request through HTTP proxy
    """
    import httpx
    target_url = f"{_BASE}{path}"

    # ZenRows API mode — detected by api.zenrows.com in the URL
    if "api.zenrows.com" in proxy_url:
        try:
            api_url = proxy_url.rstrip("&") + f"&url={target_url}"
            # Accept-Encoding: identity — tell the origin not to compress so ZenRows
            # can forward the response as plain text without codec issues
            _zr_headers = {**_HEADERS, "Accept-Encoding": "identity"}
            r = httpx.get(
                api_url,
                headers=_zr_headers,
                timeout=httpx.Timeout(connect=15.0, read=45.0, write=5.0, pool=5.0),
            )
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    # Response came back but isn't valid JSON (binary/compressed leak)
                    # Treat as no-data — don't penalise CB
                    logger.debug("Sofascore ZenRows [%s]: non-JSON 200 response — skipping", path)
                    return _NO_DATA
            if r.status_code in (404, 422):
                logger.debug("Sofascore %s via ZenRows [%s] (no data — skipping)", r.status_code, path)
                return _NO_DATA  # sentinel: request worked, content just absent
            logger.warning("Sofascore %s via ZenRows [%s]", r.status_code, path)
            return None
        except UnicodeDecodeError:
            # Binary response leaked through ZenRows — no data, not a proxy failure
            logger.debug("Sofascore ZenRows [%s]: encoding error — skipping", path)
            return _NO_DATA
        except Exception as e:
            logger.warning("Sofascore ZenRows request failed [%s]: %s", path, e)
            return None

    # Standard proxy mode
    try:
        r = httpx.get(
            target_url,
            headers=_HEADERS,
            proxies={"http://": proxy_url, "https://": proxy_url},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=5.0, pool=5.0),
            verify=False,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            logger.debug("Sofascore 404 via proxy [%s] (no games)", path)
        else:
            logger.warning("Sofascore %s via proxy [%s]", r.status_code, path)
        return None
    except Exception as e:
        logger.warning("Sofascore proxy request failed [%s]: %s", path, e)
        return None


def _get(path: str) -> dict | list | None:
    """Sofascore request via SOFASCORE_PROXY_URL (ZenRows or any HTTP proxy). Circuit breaker applies."""
    if _cb_is_open():
        return None

    import os
    sf_proxy = os.getenv("SOFASCORE_PROXY_URL", "")
    if not sf_proxy:
        logger.warning("Sofascore: SOFASCORE_PROXY_URL not set — cannot fetch [%s]", path)
        return None

    result = _generic_proxy_get(path, sf_proxy)
    if result is _NO_DATA:
        _cb_record_success()  # proxy is alive — 404/422 is content, not connectivity
        return None
    if result is not None:
        _cb_record_success()
    else:
        _cb_record_failure()
    return result


# ── Scheduled events ──────────────────────────────────────────────────────────

def get_scheduled_events(sport_key: str, date: str | None = None) -> list[dict]:
    """Return matches for a single sport_key on a given date (cached 1 hour)."""
    slug = _slug(sport_key)
    if not slug:
        return []
    date = date or et_naive().strftime("%Y-%m-%d")
    try:
        import redis as _r_gs, json as _j_gs
        from src.core.config import REDIS_URL as _ru_gs
        _rc_gs = _r_gs.from_url(_ru_gs, decode_responses=True, socket_connect_timeout=2)
        _ck_gs = f"sofascore:sport:{slug}:{date}"
        _hit = _rc_gs.get(_ck_gs)
        if _hit:
            return _j_gs.loads(_hit)
    except Exception:
        _rc_gs = None

    data = _get(f"/sport/{slug}/scheduled-events/{date}")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    result = [_normalise_event(e, sport_key) for e in events]

    try:
        if _rc_gs:
            _rc_gs.setex(_ck_gs, 3600, _j_gs.dumps(result))
    except Exception:
        pass
    return result


def get_all_scheduled_events(date: str | None = None) -> list[dict]:
    """
    Return ALL of today's events (scheduled + live) across EVERY sport worldwide.

    Scans ALL_SF_SLUGS — every sport Sofascore tracks globally including:
    soccer, basketball, NFL, NBA, MLB, NHL, tennis, cricket, rugby, MMA,
    motorsport, golf, AFL, handball, volleyball, futsal, beach volleyball,
    cycling, darts, table tennis, badminton, squash, boxing, snooker,
    esports, floorball, bandy, biathlon, ski jumping, and more.

    2 requests per slug (scheduled + live) — batched with concurrency cap
    to avoid rate-limiting.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    date = date or et_naive().strftime("%Y-%m-%d")

    # Build slug → [sport_keys] reverse map (for events that have Odds API mappings)
    slug_to_keys: dict[str, list[str]] = {}
    for sk, slug in SPORT_MAP.items():
        slug_to_keys.setdefault(slug, []).append(sk)

    # Use ALL_SF_SLUGS so sports without Odds API mapping are still scanned
    all_slugs = list(dict.fromkeys(ALL_SF_SLUGS + list(slug_to_keys.keys())))

    all_events: list[dict] = []
    seen_ids: set[str] = set()

    def _fetch(slug: str, endpoint: str) -> list[dict]:
        if _cb_is_open():
            return []
        data = _get(endpoint)
        if not data:
            return []
        raw  = data if isinstance(data, list) else data.get("events", [])
        # Use first matching Odds API sport_key if available, else use the slug itself
        key = (slug_to_keys.get(slug) or [slug])[0]
        return [_normalise_event(e, key) for e in raw]

    # Build task list: scheduled + live per slug
    tasks: list[tuple[str, str]] = []
    for slug in all_slugs:
        tasks.append((slug, f"/sport/{slug}/scheduled-events/{date}"))
        tasks.append((slug, f"/sport/{slug}/events/live"))

    # Use more workers for direct proxy (fast), fewer for ScraperAPI (slower, rate-limited)
    import os as _os
    using_scraper = bool(_os.getenv("SOFASCORE_PROXY_URL", ""))
    max_workers   = 4 if using_scraper else 8
    fut_timeout   = 60 if using_scraper else 25

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, slug, endpoint): (slug, endpoint) for slug, endpoint in tasks}
        for fut in as_completed(futures, timeout=fut_timeout * len(tasks)):
            slug, endpoint = futures[fut]
            try:
                for ev in fut.result():
                    eid = ev.get("id", "")
                    if eid and eid in seen_ids:
                        continue
                    if eid:
                        seen_ids.add(eid)
                    all_events.append(ev)
            except Exception as e:
                logger.warning("Sofascore batch [%s %s]: %s", slug, endpoint, e)

    scheduled = sum(1 for e in all_events if not e.get("status_type", "").startswith("inprogress"))
    live      = len(all_events) - scheduled
    logger.info("Sofascore batch scan: %d scheduled + %d live = %d total across %d sport slugs",
                scheduled, live, len(all_events), len(all_slugs))

    # Cache the full result for the rest of the day so this 200+ request scan is never repeated
    try:
        import redis as _r_all, json as _j_all
        from src.core.config import REDIS_URL as _ru_all
        _rc_all = _r_all.from_url(_ru_all, decode_responses=True, socket_connect_timeout=2)
        _rc_all.setex("sofascore:today_events", 86400, _j_all.dumps(all_events))
        logger.info("Sofascore batch scan cached (%d events) for 24h", len(all_events))
    except Exception:
        pass

    return all_events


def get_live_events(sport_key: str) -> list[dict]:
    """Return currently live matches."""
    slug = _slug(sport_key)
    if not slug:
        return []
    data = _get(f"/sport/{slug}/events/live")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    return [_normalise_event(e, sport_key) for e in events]


def get_event_result(event_id: str) -> dict | None:
    """
    Fetch the current/final result for a specific Sofascore event by ID.
    Returns normalised dict with home_score, away_score, status_type, winner.
    Returns None if the event can't be fetched.
    """
    data = _get(f"/event/{event_id}")
    if not data:
        return None
    ev = data.get("event", data) if isinstance(data, dict) else None
    if not ev:
        return None
    status = ev.get("status", {})
    hs = (ev.get("homeScore") or {}).get("current")
    as_ = (ev.get("awayScore") or {}).get("current")
    status_type = status.get("type", "")
    home_name = (ev.get("homeTeam") or {}).get("name", "home")
    away_name = (ev.get("awayTeam") or {}).get("name", "away")
    winner: str
    # winnerCode: 1=home wins, 2=away wins, 3=draw — always set by Sofascore
    # Use score comparison first; fall back to winnerCode for sports without
    # numeric scores (boxing, cricket innings, etc.)
    winner_code = ev.get("winnerCode")
    if hs is not None and as_ is not None and hs != as_:
        winner = home_name if hs > as_ else away_name
    elif winner_code == 1:
        winner = home_name
    elif winner_code == 2:
        winner = away_name
    elif winner_code == 3 or (hs is not None and as_ is not None and hs == as_):
        winner = "draw"
    else:
        winner = "unknown"
    return {
        "event_id":    str(event_id),
        "home_team":   (ev.get("homeTeam") or {}).get("name", ""),
        "away_team":   (ev.get("awayTeam") or {}).get("name", ""),
        "home_score":  hs,
        "away_score":  as_,
        "status_type": status_type,          # "notstarted" / "inprogress" / "finished"
        "status_desc": status.get("description", ""),
        "winner":      winner,
        "is_live":     status_type == "inprogress",
        "is_finished": status_type in (
            "finished", "ended", "afterExtraTime", "afterPenalties",
            "awaitingExtraTime", "awaitingPenalties", "retired", "walkover",
            "canceled", "postponed", "interrupted",
        ),
    }


def find_event_by_teams(home_team: str, away_team: str, date_str: str | None = None,
                        sport_key: str | None = None) -> dict | None:
    """
    Search today's (or date_str's) Sofascore events for a match between these two teams.
    Returns the normalised event dict if found, else None.
    Uses cached scan data from Redis when available.
    """
    import json as _json
    from src.core.timezone import et_naive as _et_n
    _today = _et_n().strftime("%Y-%m-%d")
    _searching_today = (date_str is None or date_str == _today)
    events: list[dict] = []
    try:
        from src.core.config import REDIS_URL
        import redis as _redis
        r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        if _searching_today:
            # Today's events: use the scan cache when available (fast path)
            cached = r.get("sofascore:today_events")
            if cached:
                events = _json.loads(cached)
        else:
            # Past date: today's cache doesn't have it — also check yesterday's DB key
            _yest_key = f"sofascore:events:{date_str}"
            _yest_raw = r.get(_yest_key)
            if _yest_raw:
                events = _json.loads(_yest_raw)
    except Exception:
        pass

    home_n = home_team.lower().strip()
    away_n = away_team.lower().strip()

    def _search(evs: list) -> dict | None:
        for ev in evs:
            eh = (ev.get("home_team") or "").lower()
            ea = (ev.get("away_team") or "").lower()
            if (home_n in eh or eh in home_n) and (away_n in ea or ea in away_n):
                if sport_key and ev.get("sport") and ev["sport"] != sport_key:
                    continue
                return ev
        return None

    # Search cached events first (fast path)
    found = _search(events) if events else None
    if found:
        return found

    # Not in cache — do ONE live scan per game per 30 min (rate-limit guard).
    # get_all_scheduled_events makes 200+ requests; never call it every 30s per pick.
    try:
        from src.core.config import REDIS_URL as _ru_fet
        import redis as _r_fet
        import json as _j_fet
        _rc_fet = _r_fet.from_url(_ru_fet, decode_responses=True, socket_connect_timeout=2)
        _miss_key = f"sofascore:miss:{home_n}:{away_n}:{date_str or 'today'}"
        if _rc_fet.get(_miss_key):
            return None  # scanned recently, still not found — skip until TTL expires
        live_events = get_all_scheduled_events(date_str)
        found = _search(live_events)
        if not found:
            _rc_fet.setex(_miss_key, 1800, "1")  # don't rescan for 30 min
        else:
            # Update the today_events cache so future lookups hit the fast path
            if _searching_today:
                try:
                    _cached_raw = _rc_fet.get("sofascore:today_events")
                    _existing = _j_fet.loads(_cached_raw) if _cached_raw else []
                    _existing.extend(live_events)
                    _rc_fet.setex("sofascore:today_events", 86400, _j_fet.dumps(_existing))
                except Exception:
                    pass
    except Exception:
        pass

    return found


def _normalise_event(e: dict, sport_key: str) -> dict:
    home = e.get("homeTeam", {})
    away = e.get("awayTeam", {})
    score = e.get("homeScore", {}), e.get("awayScore", {})
    status = e.get("status", {})
    # Capture team country — used to match Kalshi country-name subtitles
    # e.g. Kalshi "Germany vs Ecuador" → Sofascore "Bayern Munich" (country: Germany)
    home_country = (home.get("country") or {}).get("name", "")
    away_country = (away.get("country") or {}).get("name", "") or ""
    tournament = e.get("tournament", {})
    season     = e.get("season", {})
    return {
        "id":              str(e.get("id", "")),
        "sport":           sport_key,
        "home_team":       home.get("name", ""),
        "home_team_id":    str(home.get("id", "")),
        "home_country":    home_country,
        "away_team":       away.get("name", ""),
        "away_team_id":    str(away.get("id", "")),
        "away_country":    away_country,
        "home_score":      score[0].get("current"),
        "away_score":      score[1].get("current"),
        "status":          status.get("description", ""),
        "status_type":     status.get("type", ""),
        "commence_time":   _epoch_to_iso(e.get("startTimestamp")),
        "tournament":      tournament.get("name", ""),
        "tournament_id":   str(tournament.get("uniqueTournament", {}).get("id", "") or
                              tournament.get("id", "")),
        "season":          season.get("name", ""),
        "season_id":       str(season.get("id", "")),
        "source":          "sofascore",
    }


def get_active_sports_today() -> set[str]:
    """
    Check Sofascore for ALL sports that have games scheduled or live today.
    Runs in parallel across all sport_keys in SPORT_MAP.
    Returns a set of Odds API sport_keys that are active.
    Used to gate Odds API calls — only fetch for confirmed active sports.
    """
    from concurrent.futures import ThreadPoolExecutor
    today = et_naive().strftime("%Y-%m-%d")
    active: set[str] = set()

    def _check(sport_key: str) -> str | None:
        slug = SPORT_MAP.get(sport_key)
        if not slug:
            return None
        # Check scheduled events first (cheaper), then live
        data = _get(f"/sport/{slug}/scheduled-events/{today}")
        events = (data or {}).get("events", []) if isinstance(data, dict) else (data or [])
        if events:
            return sport_key
        data = _get(f"/sport/{slug}/events/live")
        events = (data or {}).get("events", []) if isinstance(data, dict) else (data or [])
        return sport_key if events else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_check, sk): sk for sk in SPORT_MAP}
        for fut in futures:
            try:
                result = fut.result()
                if result:
                    active.add(result)
            except Exception as _e:
                logger.warning("Sofascore active-sport check failed: %s", _e)

    logger.info("Sofascore active sports today: %s", sorted(active))
    return active


def _epoch_to_iso(ts: int | None) -> str:
    if ts is None:
        return ""
    try:
        # Always store as ET-aware ISO string so all consumers parse it correctly
        return datetime.fromtimestamp(ts, tz=ET).isoformat()
    except (OSError, ValueError):
        return ""


# ── Odds ──────────────────────────────────────────────────────────────────────

def _decimal_to_american(odd: float) -> str:
    """Convert decimal odds to American odds string."""
    if odd >= 2.0:
        return f"+{int((odd - 1) * 100)}"
    elif odd > 1.0:
        return str(int(-100 / (odd - 1)))
    return ""


def get_event_odds(event_id: str) -> dict:
    """
    Return pre-match odds for a Sofascore event — all available market types.
    Sofascore aggregates odds from bookmakers (Bet365 = bookmaker 1).

    Returns a dict with:
      - 1X2 moneyline: home_odds, draw_odds, away_odds (+ _implied variants)
      - Totals (over/under goals): totals list [{line, over_odds, under_odds, over_implied, under_implied}]
      - BTTS (both teams to score): btts_yes_odds, btts_no_odds (+ _implied variants)
      - Asian handicap: handicaps list [{line, home_odds, away_odds}]
    """
    data = _get(f"/event/{event_id}/odds/1/all")
    if not data:
        return {}
    result: dict = {"source": "sofascore"}
    try:
        markets = data.get("markets", []) if isinstance(data, dict) else []
        for market in markets:
            mname = (market.get("marketName") or "").lower()
            choices = market.get("choices", [])

            # ── 1X2 / Match winner ──────────────────────────────────────────
            if mname in ("1x2", "match winner", "moneyline", "full time result"):
                for c in choices:
                    name = c.get("name", "").upper()
                    odd  = float(c.get("odd") or 0)
                    if not odd or odd <= 1.0:
                        continue
                    american = _decimal_to_american(odd)
                    implied  = round(1 / odd, 4)
                    if name in ("1", "HOME"):
                        result["home_odds"]    = american
                        result["home_implied"] = implied
                    elif name in ("X", "DRAW"):
                        result["draw_odds"]    = american
                        result["draw_implied"] = implied
                    elif name in ("2", "AWAY"):
                        result["away_odds"]    = american
                        result["away_implied"] = implied

            # ── Over/Under totals (goals / points / runs) ───────────────────
            elif any(kw in mname for kw in ("over/under", "total goals", "total points",
                                             "total runs", "total score", "goals over")):
                # Each Sofascore total choice has name like "Over" / "Under" and a handicap
                over_c  = next((c for c in choices if c.get("name", "").lower() == "over"),  None)
                under_c = next((c for c in choices if c.get("name", "").lower() == "under"), None)
                if not over_c or not under_c:
                    continue
                line = float(market.get("handicap") or market.get("line") or
                             over_c.get("handicap") or 0)
                over_odd  = float(over_c.get("odd")  or 0)
                under_odd = float(under_c.get("odd") or 0)
                if not over_odd or not under_odd or over_odd <= 1.0 or under_odd <= 1.0:
                    continue
                totals_entry = {
                    "line":           line,
                    "over_odds":      _decimal_to_american(over_odd),
                    "under_odds":     _decimal_to_american(under_odd),
                    "over_implied":   round(1 / over_odd,  4),
                    "under_implied":  round(1 / under_odd, 4),
                }
                result.setdefault("totals", []).append(totals_entry)

            # ── BTTS (both teams to score) ──────────────────────────────────
            elif any(kw in mname for kw in ("both teams to score", "btts", "both to score")):
                yes_c = next((c for c in choices if c.get("name", "").lower() in ("yes", "both score")), None)
                no_c  = next((c for c in choices if c.get("name", "").lower() in ("no",  "no score")),   None)
                if yes_c and no_c:
                    yes_odd = float(yes_c.get("odd") or 0)
                    no_odd  = float(no_c.get("odd")  or 0)
                    if yes_odd > 1.0 and no_odd > 1.0:
                        result["btts_yes_odds"]    = _decimal_to_american(yes_odd)
                        result["btts_no_odds"]     = _decimal_to_american(no_odd)
                        result["btts_yes_implied"] = round(1 / yes_odd, 4)
                        result["btts_no_implied"]  = round(1 / no_odd,  4)

            # ── Asian handicap ──────────────────────────────────────────────
            elif "asian handicap" in mname or "handicap" in mname:
                home_c = next((c for c in choices if c.get("name", "").upper() in ("1", "HOME")), None)
                away_c = next((c for c in choices if c.get("name", "").upper() in ("2", "AWAY")), None)
                if home_c and away_c:
                    line     = float(market.get("handicap") or home_c.get("handicap") or 0)
                    home_odd = float(home_c.get("odd") or 0)
                    away_odd = float(away_c.get("odd") or 0)
                    if home_odd > 1.0 and away_odd > 1.0:
                        result.setdefault("handicaps", []).append({
                            "line":      line,
                            "home_odds": _decimal_to_american(home_odd),
                            "away_odds": _decimal_to_american(away_odd),
                        })

    except Exception as e:
        logger.warning("get_event_odds(%s) parse error: %s", event_id, e)

    if len(result) <= 1:  # only "source" key → nothing parsed
        return {}
    return result


# ── Head-to-head ──────────────────────────────────────────────────────────────

def get_h2h(event_id: str) -> list[dict]:
    """
    Return last N head-to-head results between the two teams in a given event.
    event_id: SofaScore event (match) ID.
    """
    data = _get(f"/event/{event_id}/h2h/events")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    results = []
    for e in events:
        home = e.get("homeTeam", {})
        away = e.get("awayTeam", {})
        hs = e.get("homeScore", {}).get("current")
        as_ = e.get("awayScore", {}).get("current")
        results.append({
            "home_team":  home.get("name", ""),
            "away_team":  away.get("name", ""),
            "home_score": hs,
            "away_score": as_,
            "date":       _epoch_to_iso(e.get("startTimestamp")),
            "winner":     (
                "home"    if (hs is not None and as_ is not None and hs > as_) else
                "away"    if (hs is not None and as_ is not None and as_ > hs) else
                "unknown" if (hs is None or as_ is None) else
                "draw"
            ),
            "source":     "sofascore",
        })
    return results


# ── Team recent form ──────────────────────────────────────────────────────────

def get_team_form(event_id: str) -> dict:
    """
    Return the recent form string for both teams in a given event.
    e.g. {"home": "WWLDW", "away": "DLWWW"}
    """
    data = _get(f"/event/{event_id}/form")
    if not data:
        return {}
    home_form = "".join(f.get("value", "?") for f in data.get("homeTeam", []))
    away_form = "".join(f.get("value", "?") for f in data.get("awayTeam", []))
    return {"home": home_form, "away": away_form, "source": "sofascore"}


def get_team_last_events(team_id: str, page: int = 0) -> list[dict]:
    """
    Return a team's most recent N completed events.
    page=0 → most recent page (≈5 matches).
    """
    data = _get(f"/team/{team_id}/events/last/{page}")
    if not data:
        return []
    events = data if isinstance(data, list) else data.get("events", [])
    results = []
    for e in events:
        home = e.get("homeTeam", {})
        away = e.get("awayTeam", {})
        hs = e.get("homeScore", {}).get("current")
        as_ = e.get("awayScore", {}).get("current")
        results.append({
            "home_team":  home.get("name", ""),
            "away_team":  away.get("name", ""),
            "home_score": hs,
            "away_score": as_,
            "date":       _epoch_to_iso(e.get("startTimestamp")),
            "source":     "sofascore",
        })
    return results


# ── Match statistics ──────────────────────────────────────────────────────────

def get_event_statistics(event_id: str) -> dict:
    """
    Return detailed in-match statistics: possession, shots, passes, etc.
    Useful for post-game CLV analysis and model calibration.
    """
    data = _get(f"/event/{event_id}/statistics")
    if not data:
        return {}

    stats_groups = data if isinstance(data, list) else data.get("statistics", [])
    result: dict = {"source": "sofascore"}
    for group in stats_groups:
        period = group.get("period", "ALL")
        for item in group.get("groups", []):
            for stat in item.get("statisticsItems", []):
                name = stat.get("name", "").lower().replace(" ", "_")
                key = f"{period}_{name}" if period != "ALL" else name
                result[key] = {
                    "home": stat.get("home"),
                    "away": stat.get("away"),
                }
    return result


# ── Player statistics ─────────────────────────────────────────────────────────

def get_player_stats(player_id: str, season_id: str) -> dict:
    """Return season statistics for a specific player."""
    data = _get(f"/player/{player_id}/statistics/season/{season_id}")
    if not data:
        return {}
    stats = data.get("statistics", data) if isinstance(data, dict) else data
    return {"player_id": player_id, "season_id": season_id,
            "stats": stats, "source": "sofascore"}


def search_player(name: str, sport_key: str) -> list[dict]:
    """Search for a player by name across all sports."""
    data = _get(f"/search/all/?q={quote(name)}")
    if not data:
        return []
    players = []
    for item in data.get("players", {}).get("hits", []):
        e = item.get("entity", {})
        team = e.get("team", {})
        players.append({
            "id":       str(e.get("id", "")),
            "name":     e.get("name", ""),
            "team":     team.get("name", ""),
            "position": e.get("position", ""),
            "source":   "sofascore",
        })
    return players


def get_player_profile(player_id: str) -> dict:
    """
    Return a comprehensive player profile from Sofascore including:
    - Basic info: name, age, nationality, position, club, jersey number
    - Attribute overview: ATT, CRE, TEC, TAC, DEF scores (0-99)
    - Strengths and weaknesses
    - Recent match history: last 5 games with rating, goals, assists, minutes
    - Season statistics: goals, assists, rating average, matches played

    Results are Redis-cached for 4 hours to avoid repeat requests.
    Returns {} if player not found.
    """
    # Redis cache check — player profiles are stable within a day
    _cache_key = f"sf:player:{player_id}"
    try:
        from src.core.config import REDIS_URL
        import redis as _rc
        import json as _jc
        _r = _rc.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _cached = _r.get(_cache_key)
        if _cached:
            return _jc.loads(_cached)
    except Exception:
        _r = None

    result: dict = {"player_id": player_id, "source": "sofascore"}

    # 1. Basic profile — name, age, nationality, club, position
    profile_data = _get(f"/player/{player_id}")
    if profile_data:
        p = profile_data.get("player", profile_data) if isinstance(profile_data, dict) else {}
        team = (p.get("team") or {})
        country = (p.get("country") or {})
        result.update({
            "name":       p.get("name", ""),
            "short_name": p.get("shortName", p.get("name", "")),
            "position":   p.get("position", ""),
            "jersey":     p.get("jerseyNumber", ""),
            "team":       team.get("name", ""),
            "team_id":    str(team.get("id", "")),
            "country":    country.get("name", ""),
            "age":        p.get("age") or p.get("dateOfBirthTimestamp"),
            "market_value": p.get("proposedMarketValue") or p.get("contractUntilTimestamp"),
        })

    # 2. Attribute overview — ATT/CRE/TEC/TAC/DEF (0-99 radar scores)
    attr_data = _get(f"/player/{player_id}/attribute-overview")
    if attr_data and isinstance(attr_data, dict):
        attrs = attr_data.get("playerAttributeOverview", attr_data)
        if isinstance(attrs, dict):
            result["attributes"] = {
                "attacking":   attrs.get("attacking"),
                "technical":   attrs.get("technical"),
                "tactical":    attrs.get("tactical"),
                "defending":   attrs.get("defending"),
                "creativity":  attrs.get("creativity"),
            }

    # 3. Characteristics — strengths, weaknesses, preferred positions
    char_data = _get(f"/player/{player_id}/characteristics")
    if char_data and isinstance(char_data, dict):
        chars = char_data.get("playerCharacteristics", char_data)
        if isinstance(chars, dict):
            result["strengths"]  = [s.get("name", "") for s in (chars.get("strengths")  or [])]
            result["weaknesses"] = [w.get("name", "") for w in (chars.get("weaknesses") or [])]
            result["positions"]  = [pos.get("position", "") for pos in (chars.get("preferredPositions") or [])]

    # 4. Recent matches — last 5 with ratings, goals, assists
    recent_data = _get(f"/player/{player_id}/events/last/0")
    if recent_data:
        events_raw = recent_data if isinstance(recent_data, list) else recent_data.get("events", [])
        recent_matches = []
        for ev in events_raw[:5]:
            home = ev.get("homeTeam", {})
            away = ev.get("awayTeam", {})
            stats = (ev.get("playerStatistics") or {})
            recent_matches.append({
                "date":        _epoch_to_iso(ev.get("startTimestamp")),
                "home_team":   home.get("name", ""),
                "away_team":   away.get("name", ""),
                "home_score":  (ev.get("homeScore") or {}).get("current"),
                "away_score":  (ev.get("awayScore") or {}).get("current"),
                "result":      (ev.get("status") or {}).get("description", ""),
                "rating":      stats.get("rating"),
                "minutes":     stats.get("minutesPlayed"),
                "goals":       stats.get("goals"),
                "assists":     stats.get("goalAssist"),
                "shots":       stats.get("onTargetScoringAttempt"),
                "key_passes":  stats.get("keyPass"),
            })
        if recent_matches:
            result["recent_matches"] = recent_matches
            # Compute quick summary: avg rating + total goals over last 5
            ratings = [m["rating"] for m in recent_matches if m.get("rating")]
            goals   = sum(m.get("goals") or 0 for m in recent_matches)
            result["last5_avg_rating"] = round(sum(ratings) / len(ratings), 2) if ratings else None
            result["last5_goals"]      = goals
            result["last5_assists"]    = sum(m.get("assists") or 0 for m in recent_matches)

    # Cache for 4 hours
    try:
        if _r and result.get("name"):
            import json as _jc2
            _r.setex(_cache_key, 4 * 3600, _jc2.dumps(result))
    except Exception:
        pass

    return result


def get_player_season_stats(player_id: str, tournament_id: str = "", season_id: str = "") -> dict:
    """
    Return a player's current-season statistics for a specific competition.
    If no season_id provided, fetches from the player's most recent season.
    Covers goals, assists, rating, matches, shots, key passes, dribbles, etc.
    """
    # Try to find current season if not provided
    if not season_id:
        seasons_data = _get(f"/player/{player_id}/statistics/seasons")
        if seasons_data and isinstance(seasons_data, dict):
            seasons = seasons_data.get("seasons", [])
            if seasons:
                # Pick most recent season
                latest = seasons[0]
                season_id    = str(latest.get("season", {}).get("id", ""))
                tournament_id = str(latest.get("uniqueTournament", {}).get("id", "") or tournament_id)

    if not season_id:
        return {}

    path = f"/player/{player_id}/statistics/season/{season_id}"
    if tournament_id:
        path = f"/player/{player_id}/tournament/{tournament_id}/season/{season_id}/statistics/overall"

    data = _get(path)
    if not data:
        return {}

    stats = data.get("statistics", data) if isinstance(data, dict) else {}
    if isinstance(stats, dict):
        return {
            "player_id":   player_id,
            "season_id":   season_id,
            "matches":     stats.get("appearances") or stats.get("matchesStarted"),
            "goals":       stats.get("goals"),
            "assists":     stats.get("assists"),
            "rating":      stats.get("rating"),
            "shots_total": stats.get("totalScoringAttempts"),
            "shots_on_tgt": stats.get("onTargetScoringAttempt"),
            "key_passes":  stats.get("keyPasses"),
            "dribbles":    stats.get("successfulDribbles"),
            "minutes":     stats.get("minutesPlayed"),
            "goals_per90": round(stats["goals"] / stats["minutesPlayed"] * 90, 3)
                           if stats.get("goals") and stats.get("minutesPlayed") else None,
            "source":      "sofascore",
        }
    return {}


def get_team_squad(team_id: str) -> list[dict]:
    """Return the full squad (players) for a team."""
    data = _get(f"/team/{team_id}/players")
    if not data:
        return []
    players = []
    for entry in data.get("players", []):
        p = entry.get("player", {})
        players.append({
            "id":       str(p.get("id", "")),
            "name":     p.get("name", ""),
            "position": p.get("position", ""),
            "jersey":   p.get("jerseyNumber", ""),
            "country":  p.get("country", {}).get("name", ""),
            "source":   "sofascore",
        })
    return players


# ── Lineups ───────────────────────────────────────────────────────────────────

def get_event_lineups(event_id: str) -> dict:
    """
    Return probable/confirmed lineups for both teams.
    Includes starting XI with jersey numbers, positions, and Sofascore ratings.
    """
    data = _get(f"/event/{event_id}/lineups")
    if not data:
        return {}
    try:
        result: dict = {"source": "sofascore"}
        for side in ("home", "away"):
            team_data = data.get(side, {})
            if not team_data:
                continue
            players = []
            for p in team_data.get("players", []):
                pi = p.get("player", {})
                players.append({
                    "name":       pi.get("name", ""),
                    "short_name": pi.get("shortName", pi.get("name", "")),
                    "position":   p.get("position", ""),
                    "jersey":     pi.get("jerseyNumber", ""),
                    "substitute": p.get("substitute", False),
                    "rating":     p.get("statistics", {}).get("rating"),
                })
            starters  = [p for p in players if not p["substitute"]]
            subs      = [p for p in players if p["substitute"]]
            formation = team_data.get("formation", "")
            result[side] = {
                "formation": formation,
                "starters":  starters,
                "subs":      subs[:6],
            }
        return result
    except Exception as e:
        logger.warning("get_event_lineups(%s) parse error: %s", event_id, e)
    return {}


def get_featured_players(event_id: str) -> dict:
    """
    Return featured/best players for a Sofascore event.
    Includes ATT, CRE, TEC, TAC, DEF ratings and overall Sofascore rating.
    """
    data = _get(f"/event/{event_id}/best-players/summary")
    if not data:
        # Try alternate endpoint
        data = _get(f"/event/{event_id}/best-players")
    if not data:
        return {}
    try:
        result: dict = {"source": "sofascore"}
        players_raw = data.get("bestPlayers", data.get("players", []))
        for side in ("home", "away"):
            side_players = [
                p for p in players_raw if p.get("side", "").lower() == side
            ] or players_raw[:2]  # fallback: first 2 if no side field
            side_list = []
            for entry in side_players[:3]:
                pi = entry.get("player", entry)
                stats = entry.get("statistics", {})
                side_list.append({
                    "name":    pi.get("name", ""),
                    "rating":  entry.get("value") or stats.get("rating"),
                    "att":     stats.get("attackingDuelsWon"),
                    "tec":     stats.get("keyPasses"),
                    "cre":     stats.get("expectedAssists"),
                    "tac":     stats.get("defensiveDuelsWon"),
                    "def":     stats.get("clearances"),
                    "goals":   stats.get("goals"),
                    "assists": stats.get("assists"),
                })
            if side_list:
                result[side] = side_list
        return result
    except Exception as e:
        logger.warning("get_featured_players(%s) parse error: %s", event_id, e)
    return {}


def get_match_trends(event_id: str) -> dict:
    """
    Return Sofascore's pre-match statistical trends for both teams.
    Includes: BTTS %, clean sheet %, under 2.5 %, first to score %,
    first half winner %, cards under %, corners under %.
    These are computed from both teams' recent match history.
    """
    data = _get(f"/event/{event_id}/votes")
    trends_data = _get(f"/event/{event_id}/statistics/pregame") or {}

    result: dict = {"source": "sofascore"}

    # votes endpoint: contains vote percentages (home/draw/away predictions by fans)
    if data and isinstance(data, dict):
        vote = data.get("vote", {})
        if vote:
            result["fan_vote"] = {
                "home_pct": vote.get("vote1"),
                "draw_pct": vote.get("voteX"),
                "away_pct": vote.get("vote2"),
            }

    # pregame statistics: BTTS, clean sheet, over/under trends
    if trends_data and isinstance(trends_data, dict):
        stats_groups = trends_data.get("statistics", [])
        for group in stats_groups:
            for item in group.get("groups", []):
                for stat in item.get("statisticsItems", []):
                    name = stat.get("name", "").lower().replace(" ", "_")
                    result[name] = {
                        "home": stat.get("home"),
                        "away": stat.get("away"),
                    }

    return result


def get_goal_distribution(event_id: str) -> dict:
    """
    Return goal distribution by time period (00-15, 15-30, 30-45, 45-60, 60-75, 75-90)
    for both teams, split by scored/conceded.
    Useful for first-half, second-half, and time-of-goal props.
    """
    data = _get(f"/event/{event_id}/statistics")
    if not data:
        return {}
    try:
        stats_groups = data if isinstance(data, list) else data.get("statistics", [])
        goal_dist: dict = {"source": "sofascore"}
        for group in stats_groups:
            period = group.get("period", "")
            for item in group.get("groups", []):
                for stat in item.get("statisticsItems", []):
                    sname = stat.get("name", "").lower()
                    if "goal" in sname or "score" in sname:
                        key = f"{period}_{sname}".replace(" ", "_").lower()
                        goal_dist[key] = {
                            "home": stat.get("home"),
                            "away": stat.get("away"),
                        }
        return goal_dist
    except Exception as e:
        logger.warning("get_goal_distribution(%s) parse error: %s", event_id, e)
    return {}


# ── Standings ─────────────────────────────────────────────────────────────────

def get_standings(tournament_id: str, season_id: str) -> list[dict]:
    """Return league/tournament standings table."""
    data = _get(f"/tournament/{tournament_id}/season/{season_id}/standings/total")
    if not data:
        return []
    rows = data if isinstance(data, list) else (data.get("standings") or [{}])[0].get("rows", [])
    result = []
    for row in rows:
        team = row.get("team", {})
        result.append({
            "position":     row.get("position"),
            "team":         team.get("name", ""),
            "team_id":      str(team.get("id", "")),
            "played":       row.get("matches"),
            "wins":         row.get("wins"),
            "draws":        row.get("draws"),
            "losses":       row.get("losses"),
            "goals_for":    row.get("scoresFor"),
            "goals_against": row.get("scoresAgainst"),
            "points":       row.get("points"),
            "source":       "sofascore",
        })
    return result


def get_team_standings_for_event(event: dict) -> dict:
    """
    Given a normalised event dict (with tournament_id + season_id), return
    standings rows for the home and away team, including their league position,
    points, wins, losses, and goals. Returns {} if standings unavailable.
    """
    tid = event.get("tournament_id", "")
    sid = event.get("season_id", "")
    if not tid or not sid:
        return {}
    try:
        rows = get_standings(tid, sid)
    except Exception:
        return {}
    if not rows:
        return {}

    home_name = (event.get("home_team") or "").lower()
    away_name = (event.get("away_team") or "").lower()
    home_row  = next((r for r in rows if home_name in r["team"].lower() or r["team"].lower() in home_name), None)
    away_row  = next((r for r in rows if away_name in r["team"].lower() or r["team"].lower() in away_name), None)

    result: dict = {"total_teams": len(rows)}
    if home_row:
        result["home_position"] = home_row.get("position")
        result["home_points"]   = home_row.get("points")
        result["home_played"]   = home_row.get("played")
        result["home_w_d_l"]    = f"{home_row.get('wins',0)}-{home_row.get('draws',0)}-{home_row.get('losses',0)}"
        result["home_gf_ga"]    = f"{home_row.get('goals_for',0)}-{home_row.get('goals_against',0)}"
    if away_row:
        result["away_position"] = away_row.get("position")
        result["away_points"]   = away_row.get("points")
        result["away_played"]   = away_row.get("played")
        result["away_w_d_l"]    = f"{away_row.get('wins',0)}-{away_row.get('draws',0)}-{away_row.get('losses',0)}"
        result["away_gf_ga"]    = f"{away_row.get('goals_for',0)}-{away_row.get('goals_against',0)}"
    return result


# ── Convenience: enrich a game context dict ───────────────────────────────────

def enrich_game_context(
    sport_key: str,
    home_team: str,
    away_team: str,
    game_time: str,
) -> dict:
    """
    Find the SofaScore event matching this game and return combined context:
    h2h history, form, standings, and last 5 events per team.

    Matches by team name substring — best-effort.
    """
    date = game_time[:10] if game_time else et_naive().strftime("%Y-%m-%d")
    events = get_scheduled_events(sport_key, date)

    matched = None
    for ev in events:
        ev_home = (ev.get("home_team") or "").lower()
        ev_away = (ev.get("away_team") or "").lower()
        if ((home_team.lower() in ev_home or ev_home in home_team.lower()) and
                (away_team.lower() in ev_away or ev_away in away_team.lower())):
            matched = ev
            break

    if not matched:
        return {"available": False, "sport": sport_key, "source": "sofascore"}

    eid = matched.get("id")
    if not eid:
        return {"available": False, "sport": sport_key, "source": "sofascore"}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as pool:
        f_h2h         = pool.submit(get_h2h, eid)
        f_form        = pool.submit(get_team_form, eid)
        f_standings   = pool.submit(get_team_standings_for_event, matched)
        f_home_last   = pool.submit(get_team_last_events, matched.get("home_team_id", ""))
        f_away_last   = pool.submit(get_team_last_events, matched.get("away_team_id", ""))
        f_stats       = pool.submit(get_event_statistics, eid)
        f_lineups     = pool.submit(get_event_lineups, eid)
        f_players     = pool.submit(get_featured_players, eid)
        f_trends      = pool.submit(get_match_trends, eid)
        f_odds        = pool.submit(get_event_odds, eid)
        # pool.__exit__ waits for all futures — safe to call .result() below

    def _get_result(f, default):
        try:
            return f.result()
        except Exception:
            return default

    context = {
        "available":       True,
        "event":           matched,
        "tournament":      matched.get("tournament", ""),
        "season":          matched.get("season", ""),
        "h2h":             _get_result(f_h2h, []),
        "form":            _get_result(f_form, {}),
        "standings":       _get_result(f_standings, {}),
        "home_last5":      _get_result(f_home_last, []),
        "away_last5":      _get_result(f_away_last, []),
        "event_stats":     _get_result(f_stats, {}),
        "lineups":         _get_result(f_lineups, {}),     # starting XI + subs + formation
        "featured_players": _get_result(f_players, {}),   # top rated players + ATT/TEC/CRE/TAC/DEF
        "match_trends":    _get_result(f_trends, {}),      # BTTS%, clean sheet%, o/u%, fan vote
        "sofascore_odds":  _get_result(f_odds, {}),        # 1X2 + totals + BTTS + handicap odds
        "source":          "sofascore",
    }
    return context
