"""Shared sport display labels and emojis — single source of truth."""

SPORT_EMOJI: dict[str, str] = {
    # US Sports
    "basketball_nba":                        "🏀",
    "basketball_wnba":                       "🏀",
    "basketball_ncaab":                      "🏀",
    "basketball_wncaab":                     "🏀",
    "baseball_mlb":                          "⚾",
    "americanfootball_nfl":                  "🏈",
    "americanfootball_ncaaf":                "🏈",
    "icehockey_nhl":                         "🏒",
    # Soccer — men's
    "soccer_epl":                            "⚽",
    "soccer_spain_la_liga":                  "⚽",
    "soccer_germany_bundesliga":             "⚽",
    "soccer_italy_serie_a":                  "⚽",
    "soccer_france_ligue_one":               "⚽",
    "soccer_usa_mls":                        "⚽",
    "soccer_netherlands_eredivisie":         "⚽",
    "soccer_portugal_primeira_liga":         "⚽",
    "soccer_mexico_ligamx":                  "⚽",
    "soccer_argentina_primera_division":     "⚽",
    "soccer_brazil_campeonato":              "⚽",
    "soccer_turkey_super_league":            "⚽",
    "soccer_spl":                            "⚽",
    "soccer_uefa_champs_league":             "⚽",
    "soccer_uefa_europa_league":             "⚽",
    "soccer_uefa_europa_conference_league":  "⚽",
    "soccer_fifa_world_cup":                 "⚽",
    "soccer_conmebol_copa_america":          "⚽",
    "soccer_conmebol_copa_libertadores":     "⚽",
    "soccer_africa_cup_of_nations":          "⚽",
    "soccer_concacaf_nations_league":        "⚽",
    # Soccer — women's
    "soccer_usa_nwsl":                       "⚽",
    "soccer_fifa_womens_world_cup":          "⚽",
    "soccer_uefa_womens_champs_league":      "⚽",
    # Golf
    "golf_pga_tour":                         "⛳",
    "golf_masters_tournament":               "⛳",
    "golf_pga_championship":                 "⛳",
    "golf_us_open":                          "⛳",
    "golf_the_open_championship":            "⛳",
    "golf_lpga":                             "⛳",
    # Combat Sports
    "mma_mixed_martial_arts":                "🥊",
    "boxing_boxing":                         "🥊",
    # Tennis — men's
    "tennis_atp_french_open":                "🎾",
    "tennis_atp_wimbledon":                  "🎾",
    "tennis_atp_us_open":                    "🎾",
    "tennis_atp_australian_open":            "🎾",
    # Tennis — women's
    "tennis_wta_french_open":                "🎾",
    "tennis_wta_wimbledon":                  "🎾",
    "tennis_wta_us_open":                    "🎾",
    "tennis_wta_australian_open":            "🎾",
    # Aussie Rules + Rugby
    "aussierules_afl":                       "🏉",
    "rugbyunion_world_cup":                  "🏉",
    "rugbyleague_nrl":                       "🏉",
}

SPORT_NAME: dict[str, str] = {
    # US Sports
    "basketball_nba":                        "NBA",
    "basketball_wnba":                       "WNBA",
    "basketball_ncaab":                      "NCAAB",
    "basketball_wncaab":                     "NCAAW",
    "baseball_mlb":                          "MLB",
    "americanfootball_nfl":                  "NFL",
    "americanfootball_ncaaf":                "NCAAF",
    "icehockey_nhl":                         "NHL",
    # Soccer — men's
    "soccer_epl":                            "EPL",
    "soccer_spain_la_liga":                  "La Liga",
    "soccer_germany_bundesliga":             "Bundesliga",
    "soccer_italy_serie_a":                  "Serie A",
    "soccer_france_ligue_one":               "Ligue 1",
    "soccer_usa_mls":                        "MLS",
    "soccer_netherlands_eredivisie":         "Eredivisie",
    "soccer_portugal_primeira_liga":         "Primeira Liga",
    "soccer_mexico_ligamx":                  "Liga MX",
    "soccer_argentina_primera_division":     "Argentine Primera",
    "soccer_brazil_campeonato":              "Brasileirão",
    "soccer_turkey_super_league":            "Süper Lig",
    "soccer_spl":                            "Scottish Prem",
    "soccer_uefa_champs_league":             "UCL",
    "soccer_uefa_europa_league":             "Europa League",
    "soccer_uefa_europa_conference_league":  "Conference League",
    "soccer_fifa_world_cup":                 "FIFA World Cup",
    "soccer_conmebol_copa_america":          "Copa América",
    "soccer_conmebol_copa_libertadores":     "Copa Libertadores",
    "soccer_africa_cup_of_nations":          "AFCON",
    "soccer_concacaf_nations_league":        "CONCACAF",
    # Soccer — women's
    "soccer_usa_nwsl":                       "NWSL",
    "soccer_fifa_womens_world_cup":          "Women's World Cup",
    "soccer_uefa_womens_champs_league":      "Women's UCL",
    # Golf
    "golf_pga_tour":                         "PGA Tour",
    "golf_masters_tournament":               "The Masters",
    "golf_pga_championship":                 "PGA Championship",
    "golf_us_open":                          "US Open (Golf)",
    "golf_the_open_championship":            "The Open",
    "golf_lpga":                             "LPGA Tour",
    # Combat Sports
    "mma_mixed_martial_arts":                "UFC/MMA",
    "boxing_boxing":                         "Boxing",
    # Tennis — men's
    "tennis_atp_french_open":                "French Open",
    "tennis_atp_wimbledon":                  "Wimbledon",
    "tennis_atp_us_open":                    "US Open (Tennis)",
    "tennis_atp_australian_open":            "Australian Open",
    # Tennis — women's
    "tennis_wta_french_open":                "French Open (W)",
    "tennis_wta_wimbledon":                  "Wimbledon (W)",
    "tennis_wta_us_open":                    "US Open (W)",
    "tennis_wta_australian_open":            "Australian Open (W)",
    # Aussie Rules + Rugby
    "aussierules_afl":                       "AFL",
    "rugbyunion_world_cup":                  "Rugby World Cup",
    "rugbyleague_nrl":                       "NRL",
}


def get_emoji(sport_key: str) -> str:
    return SPORT_EMOJI.get(sport_key, "🎯")


def get_name(sport_key: str) -> str:
    return SPORT_NAME.get(sport_key, sport_key.replace("_", " ").title())
