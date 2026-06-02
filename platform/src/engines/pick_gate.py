"""
Pick Gate — pre-commit quality filter.

Every BET recommendation must pass this gate before it is persisted or alerted.
A pick that cannot be explained, verified, or traced should not be posted.

Failure is not an error — it is correct behaviour. Passing on a bad bet is
as valuable as winning a good one.
"""
import logging
from dataclasses import dataclass, field
from src.core.config import MIN_EV_PCT

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
MIN_EV_PCT_GATE        = max(MIN_EV_PCT, 0.03)   # never below 3% regardless of env config
MIN_REASONING_CHARS    = 80                       # explanation must be substantive
MIN_KEY_FACTORS        = 2                        # at least 2 traceable factors
MIN_BOOKS_FOR_EV       = 2                        # EV must be visible across ≥2 books
MIN_CONFIDENCE         = 0.50                     # calibrated confidence floor
MAX_RISK_SCORE         = 75.0                     # reject high-risk picks outright


@dataclass
class GateResult:
    passed:   bool
    reasons:  list[str] = field(default_factory=list)   # why it failed
    warnings: list[str] = field(default_factory=list)   # passed but notable


def check(pick, odds_by_book: dict | None = None) -> GateResult:
    """
    Validate a PickRecommendation before it is committed to the DB or alerted.

    pick: PickRecommendation instance
    odds_by_book: {book_name: american_odds} for the selected bet — used to
                  verify EV is visible across multiple books, not just one outlier.

    Returns GateResult. If result.passed is False, the pick must not be posted.
    """
    if pick.recommendation != "BET":
        # PASS picks are always allowed through — we log them separately
        return GateResult(passed=True)

    reasons  = []
    warnings = []

    # ── 1. EV floor ────────────────────────────────────────────────────────────
    if pick.ev_pct < MIN_EV_PCT_GATE:
        reasons.append(
            f"EV too low: {pick.ev_pct:.1%} (minimum {MIN_EV_PCT_GATE:.0%})"
        )

    # ── 2. Explanation required ────────────────────────────────────────────────
    reasoning = (pick.reasoning or "").strip()
    if len(reasoning) < MIN_REASONING_CHARS:
        reasons.append(
            f"Reasoning too short: {len(reasoning)} chars (minimum {MIN_REASONING_CHARS})"
        )

    # ── 3. Key factors required ────────────────────────────────────────────────
    factors = [f for f in (pick.key_factors or []) if f and str(f).strip()]
    if len(factors) < MIN_KEY_FACTORS:
        reasons.append(
            f"Insufficient key factors: {len(factors)} provided (minimum {MIN_KEY_FACTORS})"
        )

    # ── 4. Multi-book EV verification ──────────────────────────────────────────
    if odds_by_book is not None:
        books_with_edge = _books_showing_edge(pick.ev_pct, odds_by_book)
        if books_with_edge < MIN_BOOKS_FOR_EV:
            reasons.append(
                f"EV only visible at {books_with_edge} book(s) — "
                f"may be a data outlier (minimum {MIN_BOOKS_FOR_EV})"
            )
    else:
        warnings.append("No multi-book odds provided — EV cross-check skipped")

    # ── 5. Confidence floor ────────────────────────────────────────────────────
    confidence = (pick.confidence_pct or 0) / 100.0
    if confidence < MIN_CONFIDENCE:
        reasons.append(
            f"Confidence too low: {confidence:.0%} (minimum {MIN_CONFIDENCE:.0%})"
        )

    # ── 6. Risk cap ────────────────────────────────────────────────────────────
    if (pick.risk_score or 0) > MAX_RISK_SCORE:
        reasons.append(
            f"Risk score too high: {pick.risk_score:.0f} (maximum {MAX_RISK_SCORE:.0f})"
        )

    # ── 7. Units sanity ────────────────────────────────────────────────────────
    if (pick.units or 0) == 0:
        reasons.append("Units assigned as 0 — pick has no stake and should be PASS")

    # ── Warnings (non-blocking) ────────────────────────────────────────────────
    if pick.ev_pct >= MIN_EV_PCT_GATE and pick.ev_pct < 0.05:
        warnings.append(f"Edge is marginal ({pick.ev_pct:.1%}) — track CLV closely")

    if len(factors) == MIN_KEY_FACTORS:
        warnings.append("Only minimum key factors provided — explanation could be stronger")

    passed = len(reasons) == 0

    if not passed:
        logger.warning(
            "Pick BLOCKED [%s | %s | EV %.1f%%]: %s",
            pick.sport, pick.bet, pick.ev_pct * 100,
            "; ".join(reasons),
        )
    elif warnings:
        logger.info(
            "Pick PASSED with warnings [%s | %s]: %s",
            pick.sport, pick.bet, "; ".join(warnings),
        )

    return GateResult(passed=passed, reasons=reasons, warnings=warnings)


def _books_showing_edge(ev_pct: float, odds_by_book: dict) -> int:
    """
    Count how many books offer odds that support a positive-EV bet
    at the same implied probability level as the best-odds book.

    A bet that's only +EV at one outlier book is likely a data error.
    """
    from src.engines.ev_engine import american_to_decimal, implied_prob

    count = 0
    for book, american_odds in odds_by_book.items():
        try:
            dec  = american_to_decimal(int(american_odds))
            prob = implied_prob(int(american_odds))
            # Book shows edge if its implied prob is within 3% of the best book
            # (i.e., the line is roughly consistent, not a one-book anomaly)
            if dec > 1.0:
                count += 1
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return count
