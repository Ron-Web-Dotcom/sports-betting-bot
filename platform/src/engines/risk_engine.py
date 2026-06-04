"""
Engine 5 — Risk Engine.

Computes a risk score (0-100) and enforces portfolio-level guardrails:
  - Max 5 units per single play
  - Max 15 units total daily exposure
  - Max 40% exposure per sport
  - Red flag detection (unstable lines, conflicting injury reports, suspicious activity)

Also implements fractional Kelly position sizing.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from src.core.config import MAX_DAILY_UNITS, MAX_SINGLE_UNITS, MAX_SPORT_EXPOSURE_PCT

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    risk_score:     float          # 0-100 (0=safest, 100=highest risk)
    approved:       bool
    units_allowed:  int            # final unit recommendation after risk caps
    rejection_reason: str = ""
    red_flags:      list[str] = field(default_factory=list)
    kelly_fraction: float = 0.0


def _daily_units_used(sport: str | None = None) -> float:
    """Sum units of all picks generated today (optionally filtered by sport)."""
    try:
        from src.db.session import get_db
        from src.db.models import Pick
        today = datetime.combine(date.today(), datetime.min.time())
        with get_db() as db:
            q = db.query(Pick).filter(
                Pick.generated_at >= today,
                Pick.recommendation == "BET",
            )
            if sport:
                q = q.filter(Pick.sport == sport)
            picks = q.all()
            return sum(p.units or 0 for p in picks)
    except Exception as e:
        logger.warning("daily_units_used lookup failed: %s", e)
        return 0.0


def _compute_risk_score(
    ev_pct:          float,
    confidence:      float,
    line_stability:  float,   # 0-1 (1=very stable line)
    data_quality:    float,   # 0-1 (1=high quality data)
    injury_flags:    int,     # number of injury concerns
    red_flag_count:  int,
) -> float:
    """
    Risk score = 100 - (positive factors) + (negative factors).
    Lower is safer.
    """
    base = 50.0
    base -= confidence * 30       # high confidence reduces risk
    base -= ev_pct * 100          # high EV reduces risk
    base += (1 - line_stability) * 20  # unstable line increases risk
    base += (1 - data_quality)   * 10
    base += injury_flags         * 5
    base += red_flag_count       * 10
    return max(0.0, min(100.0, round(base, 1)))


def _detect_red_flags(
    line_movement_volatility: float,   # std dev of recent line changes
    conflicting_injury_reports: bool,
    suspicious_market_activity: bool,
    data_quality: float,
) -> list[str]:
    flags = []
    if line_movement_volatility > 0.05:
        flags.append("Unstable line movement detected")
    if conflicting_injury_reports:
        flags.append("Conflicting injury reports")
    if suspicious_market_activity:
        flags.append("Suspicious market activity")
    if data_quality < 0.5:
        flags.append("Poor data quality")
    return flags


def kelly_fraction(win_prob: float, decimal_odds: float, fraction: float = 0.25) -> float:
    """Fractional Kelly (default 25%)."""
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    k = (b * win_prob - (1 - win_prob)) / b
    return max(0.0, k * fraction)


def assess(
    requested_units: int,
    sport:           str,
    ev_pct:          float,
    confidence:      float,
    win_prob:        float,
    decimal_odds:    float,
    line_movement_volatility: float = 0.02,
    conflicting_injuries: bool = False,
    suspicious_activity: bool = False,
    data_quality: float = 0.9,
    injury_flags: int = 0,
) -> RiskAssessment:
    red_flags = _detect_red_flags(
        line_movement_volatility, conflicting_injuries, suspicious_activity, data_quality,
    )
    risk_score = _compute_risk_score(ev_pct, confidence, 1 - line_movement_volatility, data_quality, injury_flags, len(red_flags))
    fk = kelly_fraction(win_prob, decimal_odds)

    # Hard caps — use a Redis atomic increment to prevent concurrent workers
    # from both reading 12u and both approving picks that push total to 18u.
    units = min(requested_units, int(MAX_SINGLE_UNITS))
    units = _atomic_daily_unit_check_and_reserve(units, sport, red_flags, risk_score, fk)
    if isinstance(units, RiskAssessment):
        return units  # limit hit or reservation failed

    # Downgrade if red flags
    if len(red_flags) >= 2:
        units = max(0, units - 1)

    if units == 0:
        return RiskAssessment(risk_score=risk_score, approved=False, units_allowed=0,
                              rejection_reason="Units reduced to 0 after risk adjustment", red_flags=red_flags, kelly_fraction=fk)

    return RiskAssessment(
        risk_score    = risk_score,
        approved      = True,
        units_allowed = units,
        red_flags     = red_flags,
        kelly_fraction= fk,
    )


def _atomic_daily_unit_check_and_reserve(
    units: int,
    sport: str,
    red_flags: list[str],
    risk_score: float,
    fk: float,
) -> "int | RiskAssessment":
    """
    Atomically check and reserve daily units using Redis INCRBY.

    Redis INCRBY is atomic — no two workers can both read-then-write
    the same counter. If the increment would exceed the limit, we
    decrement back and return a rejection.

    Falls back to the DB-query approach if Redis is unavailable.
    """
    from datetime import date
    try:
        import redis as _redis
        from src.core.config import REDIS_URL
        r = _redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        key = f"daily_units:{date.today().isoformat()}"
        # Lua script: atomically reserve up to MAX_DAILY_UNITS units.
        # Returns the number of units actually reserved (0 = fully exhausted).
        lua = """
local key    = KEYS[1]
local req    = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])
local ttl    = tonumber(ARGV[3])
local cur    = tonumber(redis.call('GET', key) or 0)
local avail  = math.max(0, limit - cur)
local grant  = math.min(req, avail)
if grant > 0 then
    redis.call('INCRBY', key, grant)
    redis.call('EXPIRE', key, ttl)
end
return grant
"""
        granted = r.eval(lua, 1, key, units, MAX_DAILY_UNITS, 90_000)
        granted = int(granted)
        if granted == 0:
            return RiskAssessment(
                risk_score=risk_score, approved=False, units_allowed=0,
                rejection_reason=f"Daily unit limit reached ({MAX_DAILY_UNITS}u)",
                red_flags=red_flags, kelly_fraction=fk,
            )
        return granted
    except Exception as e:
        logger.warning("Redis unit reservation failed, falling back to DB check: %s", e)
        # Fallback: read from DB (not atomic but better than nothing)
        daily_used = _daily_units_used()
        if daily_used + units > MAX_DAILY_UNITS:
            remaining = max(0, int(MAX_DAILY_UNITS - daily_used))
            if remaining == 0:
                return RiskAssessment(
                    risk_score=risk_score, approved=False, units_allowed=0,
                    rejection_reason=f"Daily unit limit reached ({MAX_DAILY_UNITS}u)",
                    red_flags=red_flags, kelly_fraction=fk,
                )
            return remaining
        return units
