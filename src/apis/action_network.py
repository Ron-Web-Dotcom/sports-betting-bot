"""
Action Network public data adapter.

Action Network (actionnetwork.com) publishes:
  - Public betting consensus (% of bets / % of money on each side)
  - Line history
  - Consensus picks

High public % on one side = fading opportunity (public money loses long-term).
High money % diverging from bet % = sharp/whale action on the other side.
"""
import logging
from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://api.actionnetwork.com/web/v1"

SPORT_MAP = {
    "americanfootball_nfl": "NFL",
    "basketball_nba":       "NBA",
    "baseball_mlb":         "MLB",
    "icehockey_nhl":        "NHL",
    "basketball_ncaab":     "NCAAB",
    "americanfootball_ncaaf": "NCAAF",
}


def get_consensus(sport_key: str) -> list[dict]:
    """
    Fetch public betting consensus for all games in a sport.
    Returns bet% and money% on each side — divergence signals sharp action.
    """
    sport = SPORT_MAP.get(sport_key)
    if not sport:
        logger.debug("action_network: unsupported sport_key %s", sport_key)
        return []
    data = get_json(f"{_BASE}/games", params={"sport": sport, "bookIds": "15,30,76"})
    if not data:
        return []

    games = []
    for game in data.get("games", []):
        teams = game.get("teams", [])
        if len(teams) < 2:
            continue

        consensus_data = {
            "game_id":   game.get("id"),
            "sport":     sport_key,
            "home":      next((t.get("full_name") for t in teams if t.get("is_home")), ""),
            "away":      next((t.get("full_name") for t in teams if not t.get("is_home")), ""),
            "start_time": game.get("start_time"),
            "lines":     [],
            "source":    "action_network",
        }

        for line_type in ("moneyline", "spread", "total"):
            bets = game.get("consensus", {}).get(line_type, {})
            if bets:
                consensus_data["lines"].append({
                    "market":    line_type,
                    "home_bets_pct":  bets.get("home", {}).get("bets_pct"),
                    "away_bets_pct":  bets.get("away", {}).get("bets_pct"),
                    "home_money_pct": bets.get("home", {}).get("money_pct"),
                    "away_money_pct": bets.get("away", {}).get("money_pct"),
                })

        games.append(consensus_data)
    return games


def detect_sharp_action(consensus: dict) -> list[str]:
    """
    Identify sharp money signals from a consensus dict.
    Classic sharp signal: public heavily on one side, but money% on the other.
    """
    signals = []
    for line in consensus.get("lines", []):
        home_bets  = line.get("home_bets_pct") or 0
        away_bets  = line.get("away_bets_pct") or 0
        home_money = line.get("home_money_pct") or 0
        away_money = line.get("away_money_pct") or 0
        market     = line.get("market", "")

        # Sharp on away: public bets home, money flows away
        if home_bets > 65 and away_money > home_money + 15:
            signals.append(
                f"Sharp money on {consensus.get('away')} {market}: "
                f"{home_bets:.0f}% of bets on home but {away_money:.0f}% of money away"
            )

        # Sharp on home: public bets away, money flows home
        if away_bets > 65 and home_money > away_money + 15:
            signals.append(
                f"Sharp money on {consensus.get('home')} {market}: "
                f"{away_bets:.0f}% of bets on away but {home_money:.0f}% of money home"
            )

    return signals
