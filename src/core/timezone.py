"""
Timezone utilities — all times are Eastern (America/New_York).

Use et_now() everywhere instead of datetime.utcnow() so timestamps,
DB queries, and display times are all consistently in ET.
"""
from datetime import datetime
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
    from datetime import timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def et_day_bounds(days_ago: int = 0) -> tuple[datetime, datetime]:
    """
    Return (start, end) naive datetimes for an ET calendar day, expressed in
    naive form matching UTC-stored DB columns.

    days_ago=0 → today ET, days_ago=1 → yesterday ET, etc.
    """
    from datetime import timedelta, timezone
    now  = datetime.now(ET)
    day  = (now - timedelta(days=days_ago)).date()
    start = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)
    end   = datetime(day.year, day.month, day.day + 1, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end
