"""
UFC Stats adapter — free public data, no API key required.

Endpoint: http://ufcstats.com/statistics/fighters
Provides: fighter record (W-L-D), KO%, submission%, decision%, reach, stance.

Used for MMA prop and ML pick enrichment when TheSportsDB player profile
doesn't have enough fight stats.
"""
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)

_BASE = "http://ufcstats.com"


def _get_html(path: str) -> str | None:
    from src.apis.base import get_html
    return get_html(f"{_BASE}{path}")


def search_fighter(name: str) -> dict:
    """
    Search UFC Stats for a fighter by name.
    Returns record, finish rates, reach, stance.
    Falls back to {} if not found or blocked.
    """
    try:
        import re
        parts = name.strip().split()
        if not parts:
            return {}
        # UFC Stats search uses first letter of last name
        last_initial = parts[-1][0].lower() if len(parts) > 1 else parts[0][0].lower()
        html = _get_html(f"/statistics/fighters?char={last_initial}&page=all")
        if not html:
            return {}

        # Find the fighter row — look for their name as a link
        name_lower = name.lower()
        # Pattern: <a href="/fighter-details/...">First Last</a>
        pattern = re.compile(
            r'href="(/fighter-details/[^"]+)"[^>]*>\s*([^<]*' +
            re.escape(parts[-1]) + r'[^<]*)</a>',
            re.IGNORECASE,
        )
        match = pattern.search(html)
        if not match:
            return {}

        fighter_url  = match.group(1)
        fighter_name = match.group(2).strip()

        # Fetch fighter detail page
        detail_html = _get_html(fighter_url)
        if not detail_html:
            return {"name": fighter_name, "source": "ufcstats"}

        def _extract(label: str) -> str:
            m = re.search(
                rf'{re.escape(label)}[^<]*</i>\s*<span[^>]*>([^<]+)</span>',
                detail_html, re.IGNORECASE,
            )
            return m.group(1).strip() if m else ""

        record_m = re.search(r'(\d+)-(\d+)-(\d+)', detail_html)
        wins = int(record_m.group(1)) if record_m else 0
        losses = int(record_m.group(2)) if record_m else 0
        draws = int(record_m.group(3)) if record_m else 0

        return {
            "name":        fighter_name,
            "record":      f"{wins}-{losses}-{draws}",
            "wins":        wins,
            "losses":      losses,
            "draws":       draws,
            "reach":       _extract("Reach"),
            "stance":      _extract("Stance"),
            "source":      "ufcstats",
        }
    except Exception as e:
        logger.debug("UFC Stats search failed for '%s': %s", name, e)
        return {}
