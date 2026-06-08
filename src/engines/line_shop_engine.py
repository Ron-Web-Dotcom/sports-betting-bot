"""
Line Shop Engine — cross-book prop comparison (PrizePicks vs Underdog).

Finds the same player+stat on both books and flags when lines differ.
A half-point gap is already edge; a full point is a gift.

Workflow:
  1. Normalise subject names and stat labels from both books
  2. Match props by (normalised_name, normalised_stat)
  3. Score each discrepancy: gap size × confidence
  4. Return ranked LineShopOpportunity list
"""
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Minimum line gap to report (half a point)
MIN_GAP = 0.5


@dataclass
class LineShopOpportunity:
    subject:     str
    stat:        str
    sport_key:   str
    pp_line:     float
    ud_line:     float
    gap:         float          # abs(pp_line - ud_line)
    best_book:   str            # "prizepicks" | "underdog"
    best_line:   float          # the more favourable line
    direction:   str            # "over" | "under" — which side gets the edge
    edge_note:   str            # human-readable explanation
    team:        str  = ""
    opponent:    str  = ""
    game_time:   str  = ""


def _normalise_name(name: str) -> str:
    """Lower, strip punctuation, collapse whitespace."""
    name = name.lower().strip()
    name = re.sub(r"['\.\-]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def _normalise_stat(stat: str) -> str:
    """Lower and strip — 'Passing Yards' == 'passing yards'."""
    return stat.lower().strip()


def _stat_aliases() -> dict[str, str]:
    """Map common Underdog stat names → PrizePicks equivalents."""
    return {
        "pass yds":        "passing yards",
        "pass yards":      "passing yards",
        "rush yds":        "rushing yards",
        "rush yards":      "rushing yards",
        "rec yds":         "receiving yards",
        "recv yards":      "receiving yards",
        "receiving yards": "receiving yards",
        "pts":             "points",
        "reb":             "rebounds",
        "ast":             "assists",
        "3-pt made":       "3-pointers made",
        "3pt made":        "3-pointers made",
        "strikeouts":      "pitcher strikeouts",
        "hits":            "hits allowed",
        "goals":           "goals scored",
        "shots":           "shots on goal",
    }


def find_discrepancies(
    pp_props: list[dict],
    ud_props: list[dict],
    min_gap: float = MIN_GAP,
) -> list[LineShopOpportunity]:
    """
    Cross-reference PrizePicks and Underdog props.
    Returns opportunities sorted by gap (largest first).
    """
    aliases = _stat_aliases()

    def _canon_stat(s: str) -> str:
        n = _normalise_stat(s)
        return aliases.get(n, n)

    # Index Underdog by (name, stat)
    ud_index: dict[tuple, dict] = {}
    for p in ud_props:
        key = (_normalise_name(p.get("subject", "")), _canon_stat(p.get("stat", "")))
        if key[0] and key[1]:
            ud_index[key] = p

    opportunities: list[LineShopOpportunity] = []

    for pp in pp_props:
        name_key = _normalise_name(pp.get("subject", ""))
        stat_key = _canon_stat(pp.get("stat", ""))
        if not name_key or not stat_key:
            continue

        ud = ud_index.get((name_key, stat_key))
        if ud is None:
            continue

        try:
            pp_line = float(pp.get("line", 0))
            ud_line = float(ud.get("line", 0))
        except (TypeError, ValueError):
            continue

        gap = abs(pp_line - ud_line)
        if gap < min_gap:
            continue

        # Determine which direction benefits from the discrepancy
        # If PP line is HIGHER than UD line:
        #   - Take OVER on Underdog (lower bar) ✓
        #   - Take UNDER on PrizePicks (higher bar) ✓
        if pp_line > ud_line:
            best_book  = "underdog"
            best_line  = ud_line
            direction  = "over"
            edge_note  = (
                f"PP line is {gap:.1f} pts higher ({pp_line}) — "
                f"bet OVER on Underdog ({ud_line}) for easier target"
            )
        else:
            best_book  = "prizepicks"
            best_line  = pp_line
            direction  = "over"
            edge_note  = (
                f"Underdog line is {gap:.1f} pts higher ({ud_line}) — "
                f"bet OVER on PrizePicks ({pp_line}) for easier target"
            )

        sport_key = pp.get("sport_key") or ud.get("sport_key", "")

        opportunities.append(LineShopOpportunity(
            subject   = pp.get("subject", ""),
            stat      = pp.get("stat", ""),
            sport_key = sport_key,
            pp_line   = pp_line,
            ud_line   = ud_line,
            gap       = round(gap, 2),
            best_book = best_book,
            best_line = best_line,
            direction = direction,
            edge_note = edge_note,
            team      = pp.get("team", "") or ud.get("team", ""),
            opponent  = pp.get("opponent", "") or ud.get("opponent", ""),
            game_time = pp.get("game_time", "") or ud.get("game_time", ""),
        ))

    opportunities.sort(key=lambda o: o.gap, reverse=True)
    logger.info(
        "Line shop: %d PP props × %d UD props → %d discrepancies (gap ≥ %.1f)",
        len(pp_props), len(ud_props), len(opportunities), min_gap,
    )
    return opportunities
