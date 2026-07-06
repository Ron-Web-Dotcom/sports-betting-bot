"""
Timezone utilities — all times are Eastern (America/New_York).

Use et_now() everywhere instead of datetime.utcnow() so timestamps,
DB queries, and display times are all consistently in ET.
"""
from datetime import datetime, UTC
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def et_now() -> datetime:
    """Return current Eastern time (timezone-aware)."""
    return datetime.now(ET)


def et_naive() -> datetime:
    """Return current Eastern time without tzinfo — for DB columns stored as naive datetime."""
    return datetime.now(ET).replace(tzinfo=None)


def to_et(dt: datetime) -> datetime:
    """Convert any datetime to Eastern time. Treats naive datetimes as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def et_day_bounds(days_ago: int = 0) -> tuple[datetime, datetime]:
    """
    Return (start, end) naive ET datetimes for an ET calendar day.
    Matches DB columns stored as naive ET strings via et_naive().

    days_ago=0 → today ET, days_ago=1 → yesterday ET, etc.
    """
    from datetime import timedelta
    now  = datetime.now(ET)
    day  = (now - timedelta(days=days_ago)).date()
    start = datetime(day.year, day.month, day.day)       # midnight ET naive
    end   = start + timedelta(days=1)
    return start, end
