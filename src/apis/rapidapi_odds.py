"""
RapidAPI fallback — REMOVED.

All three RapidAPI subscriptions (Sportsbook API, OddsPapi, Free Livescore)
hit their free-tier quota limits and were removed. The Odds API credits reset
on July 10, 2026 — scans resume automatically at that point.
"""


def is_available() -> bool:
    return False


def fetch_events(sport_key: str) -> list[dict]:
    return []


def get_active_sports() -> set[str]:
    return set()
