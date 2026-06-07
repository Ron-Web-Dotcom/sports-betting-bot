#!/usr/bin/env python3
"""
games_today.py — show every game happening today across all sports.

Usage:
    python games_today.py           # all sports
    python games_today.py --live    # only in-progress games
    python games_today.py nba nfl   # specific sports only
"""
import sys
import os

# Allow running from platform/ dir without installing the package
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("USE_SQLITE", "true")
os.environ.setdefault("OPENAI_API_KEY", "dummy")
os.environ.setdefault("ODDS_API_KEY",   "dummy")

from concurrent.futures import ThreadPoolExecutor, as_completed
from src.apis.espn import fetch_scoreboard, SPORT_MAP

# Friendly display names
_LABELS = {
    "basketball_nba":                  "🏀 NBA",
    "americanfootball_nfl":            "🏈 NFL",
    "baseball_mlb":                    "⚾ MLB",
    "icehockey_nhl":                   "🏒 NHL",
    "soccer_epl":                      "⚽ Premier League",
    "soccer_uefa_champs_league":       "⚽ Champions League",
    "soccer_usa_mls":                  "⚽ MLS",
    "basketball_ncaab":                "🏀 NCAAB",
    "americanfootball_ncaaf":          "🏈 NCAAF",
    "mma":                             "🥊 UFC/MMA",
    "tennis":                          "🎾 Tennis (ATP)",
    "golf_masters_tournament_winner":  "⛳ Golf (PGA)",
}

_STATUS_LIVE = {"in progress", "halftime", "in-progress", "live", "end of period"}


def _score_line(game: dict) -> str:
    hs = game.get("home_score")
    as_ = game.get("away_score")
    if hs is not None and as_ is not None and str(hs) and str(as_):
        return f"  {game['away_team']} {as_} @ {game['home_team']} {hs}"
    return f"  {game['away_team']} @ {game['home_team']}"


def _status_tag(game: dict) -> str:
    s = (game.get("status") or "").strip()
    if not s:
        return ""
    sl = s.lower()
    if any(k in sl for k in _STATUS_LIVE):
        return f"\033[92m[LIVE — {s}]\033[0m"   # green
    if game.get("completed"):
        return f"\033[90m[Final]\033[0m"          # grey
    return f"\033[94m[{s}]\033[0m"               # blue = scheduled


def run(sport_filter: list[str] | None = None, live_only: bool = False):
    keys = list(SPORT_MAP.keys())
    if sport_filter:
        keys = [k for k in keys if any(f in k for f in sport_filter)]

    results: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        futures = {pool.submit(fetch_scoreboard, k): k for k in keys}
        for future in as_completed(futures, timeout=20):
            k = futures[future]
            try:
                results[k] = future.result()
            except Exception:
                results[k] = []

    total = 0
    live  = 0
    for key in keys:
        games = results.get(key, [])
        if not games:
            continue
        if live_only:
            games = [g for g in games if any(
                kw in (g.get("status") or "").lower() for kw in _STATUS_LIVE
            )]
        if not games:
            continue

        label = _LABELS.get(key, key)
        print(f"\n{label}")
        print("─" * 45)
        for g in games:
            tag  = _status_tag(g)
            line = _score_line(g)
            print(f"{line}  {tag}")
            total += 1
            if any(kw in (g.get("status") or "").lower() for kw in _STATUS_LIVE):
                live += 1

    if total == 0:
        print("\nNo games found right now.")
    else:
        print(f"\n{'─'*45}")
        print(f"Total: {total} games  |  Live: {live}")


if __name__ == "__main__":
    args       = sys.argv[1:]
    live_only  = "--live" in args
    sport_args = [a for a in args if not a.startswith("--")]
    run(sport_filter=sport_args or None, live_only=live_only)
