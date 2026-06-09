"""Shared sport display labels and emojis — single source of truth."""

SPORT_EMOJI: dict[str, str] = {
    # US Sports
    "basketball_nba":               "🏀",
    "basketball_wnba":              "🏀",
    "basketball_ncaab":             "🏀",
    "basketball_wncaab":            "🏀",
    "baseball_mlb":                 "⚾",
    "americanfootball_nfl":         "🏈",
    "americanfootball_ncaaf":       "🏈",
    "icehockey_nhl":                "🏒",
    # Soccer
    "soccer_epl":                   "⚽",
    "soccer_spain_la_liga":         "⚽",
    "soccer_germany_bundesliga":    "⚽",
    "soccer_italy_serie_a":         "⚽",
    "soccer_france_ligue_one":      "⚽",
    "soccer_usa_mls":               "⚽",
    "soccer_usa_nwsl":              "⚽",
    "soccer_netherlands_eredivisie":"⚽",
    "soccer_portugal_primeira_liga":"⚽",
    "soccer_uefa_champs_league":    "⚽",
    "soccer_uefa_europa_league":    "⚽",
    "soccer_fifa_world_cup":        "⚽",
    "soccer_fifa_womens_world_cup": "⚽",
    "soccer_concacaf_nations_league":"⚽",
    "soccer_conmebol_copa_america": "⚽",
    "soccer_conmebol_copa_libertadores": "⚽",
    # Combat
    "mma_mixed_martial_arts":       "🥊",
    # Tennis
    "tennis_atp_french_open":       "🎾",
    "tennis_wta_french_open":       "🎾",
}

SPORT_NAME: dict[str, str] = {
    # US Sports
    "basketball_nba":               "NBA",
    "basketball_wnba":              "WNBA",
    "basketball_ncaab":             "NCAAB",
    "basketball_wncaab":            "NCAAW",
    "baseball_mlb":                 "MLB",
    "americanfootball_nfl":         "NFL",
    "americanfootball_ncaaf":       "NCAAF",
    "icehockey_nhl":                "NHL",
    # Soccer
    "soccer_epl":                   "EPL",
    "soccer_spain_la_liga":         "La Liga",
    "soccer_germany_bundesliga":    "Bundesliga",
    "soccer_italy_serie_a":         "Serie A",
    "soccer_france_ligue_one":      "Ligue 1",
    "soccer_usa_mls":               "MLS",
    "soccer_usa_nwsl":              "NWSL",
    "soccer_netherlands_eredivisie":"Eredivisie",
    "soccer_portugal_primeira_liga":"Primeira Liga",
    "soccer_uefa_champs_league":    "UCL",
    "soccer_uefa_europa_league":    "Europa League",
    "soccer_fifa_world_cup":        "World Cup",
    "soccer_fifa_womens_world_cup": "Women's World Cup",
    "soccer_concacaf_nations_league":"CONCACAF",
    "soccer_conmebol_copa_america": "Copa América",
    "soccer_conmebol_copa_libertadores": "Copa Libertadores",
    # Combat
    "mma_mixed_martial_arts":       "UFC/MMA",
    # Tennis
    "tennis_atp_french_open":       "French Open (ATP)",
    "tennis_wta_french_open":       "French Open (WTA)",
}


def get_emoji(sport_key: str) -> str:
    return SPORT_EMOJI.get(sport_key, "🎯")


def get_name(sport_key: str) -> str:
    return SPORT_NAME.get(sport_key, sport_key.replace("_", " ").title())
