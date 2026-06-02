"""
Weather adapter — Open-Meteo (free, no API key).

Outdoor sports (NFL, MLB, Soccer, Golf) are materially affected by:
  - Wind speed > 15mph: reduces passing/kicking efficiency, depresses totals
  - Precipitation: increases fumbles, reduces scoring
  - Temperature < 30°F: reduces scoring in NFL
  - Temperature > 95°F: fatigue factor in MLB
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER = "https://api.open-meteo.com/v1/forecast"

# Sports where weather materially affects outcomes
WEATHER_RELEVANT = {
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "baseball_mlb",
    "soccer_epl",
    "soccer_usa_mls",
    "golf_masters_tournament_winner",
}

# Known NFL/MLB venue coordinates (lat, lon) — expand as needed
VENUE_COORDS: dict[str, tuple[float, float]] = {
    "Lambeau Field":           (44.5013, -88.0622),
    "Soldier Field":           (41.8623, -87.6167),
    "Highmark Stadium":        (42.7738, -78.7870),
    "MetLife Stadium":         (40.8135, -74.0744),
    "Gillette Stadium":        (42.0909, -71.2643),
    "Arrowhead Stadium":       (39.0489, -94.4839),
    "Wrigley Field":           (41.9484, -87.6553),
    "Fenway Park":             (42.3467, -71.0972),
    "Yankee Stadium":          (40.8296, -73.9262),
    "Dodger Stadium":          (34.0739, -118.2400),
    "Oracle Park":             (37.7786, -122.3893),
}


def _geocode(city: str) -> tuple[float, float] | None:
    data = get_json(_GEOCODE, params={"name": city, "count": 1, "language": "en"})
    if not data or not data.get("results"):
        return None
    r = data["results"][0]
    return r.get("latitude"), r.get("longitude")


def get_game_weather(venue_or_city: str, game_time_iso: str) -> dict:
    """
    Fetch weather forecast for a game location and time.
    venue_or_city: venue name (checked against VENUE_COORDS) or city name
    game_time_iso: ISO datetime string
    """
    coords = VENUE_COORDS.get(venue_or_city)
    if not coords:
        coords = _geocode(venue_or_city)
    if not coords:
        return {"available": False, "venue": venue_or_city}

    lat, lon = coords
    data = get_json(_WEATHER, params={
        "latitude":         lat,
        "longitude":        lon,
        "hourly":           "temperature_2m,precipitation,windspeed_10m,weathercode",
        "temperature_unit": "fahrenheit",
        "windspeed_unit":   "mph",
        "forecast_days":    3,
        "timezone":         "auto",
    })

    if not data:
        return {"available": False, "lat": lat, "lon": lon}

    # Find the hour closest to game time
    hours = data.get("hourly", {})
    times = hours.get("time", [])
    temps = hours.get("temperature_2m", [])
    precip = hours.get("precipitation", [])
    winds  = hours.get("windspeed_10m", [])
    codes  = hours.get("weathercode", [])

    game_hour = game_time_iso[:13]   # "2024-01-15T18"
    idx = next((i for i, t in enumerate(times) if t.startswith(game_hour)), 0)

    temp  = temps[idx]  if idx < len(temps)  else None
    wind  = winds[idx]  if idx < len(winds)  else None
    rain  = precip[idx] if idx < len(precip) else None
    code  = codes[idx]  if idx < len(codes)  else None

    return {
        "available":       True,
        "venue":           venue_or_city,
        "temperature_f":   temp,
        "wind_mph":        wind,
        "precipitation_mm": rain,
        "weather_code":    code,
        "impact":          _assess_impact(temp, wind, rain),
        "source":          "open-meteo",
    }


def _assess_impact(temp_f, wind_mph, precip_mm) -> dict:
    """Translate raw weather into betting-relevant signals."""
    factors = []
    total_impact = 0.0   # negative = depresses scoring / totals

    if wind_mph and wind_mph > 20:
        factors.append(f"High wind {wind_mph:.0f}mph — depresses passing/kicking")
        total_impact -= 0.15
    elif wind_mph and wind_mph > 12:
        factors.append(f"Moderate wind {wind_mph:.0f}mph — slight pass game impact")
        total_impact -= 0.07

    if temp_f and temp_f < 20:
        factors.append(f"Extreme cold {temp_f:.0f}F — significant scoring reduction")
        total_impact -= 0.20
    elif temp_f and temp_f < 32:
        factors.append(f"Below freezing {temp_f:.0f}F — moderate scoring reduction")
        total_impact -= 0.10

    if precip_mm and precip_mm > 5:
        factors.append(f"Heavy precipitation {precip_mm:.1f}mm — ball security issues")
        total_impact -= 0.12
    elif precip_mm and precip_mm > 1:
        factors.append(f"Light precipitation — minor impact")
        total_impact -= 0.04

    if temp_f and temp_f > 95:
        factors.append(f"Extreme heat {temp_f:.0f}F — fatigue factor in later innings/quarters")
        total_impact -= 0.05

    return {
        "factors":        factors,
        "total_adjustment": round(total_impact, 3),
        "bet_under":      total_impact < -0.10,   # signal to lean under on totals
    }
