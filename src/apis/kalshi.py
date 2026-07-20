"""
Kalshi adapter.

Kalshi is a regulated prediction/event contracts market covering sports,
politics, economics, and more. Sports markets include NFL, NBA, MLB, NHL,
NCAAB, NCAAF, Tennis, Golf, Soccer, UFC, and F1.

Official public API with full documentation at docs.kalshi.com.
Base: https://external-api.kalshi.com/trade-api/v2

Auth: RSA-PSS SHA256 signature
  Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
  Sign: timestamp(ms) + METHOD + path  with your RSA private key

Set env vars:
  KALSHI_API_KEY_ID     — your API key ID from dashboard
  KALSHI_PRIVATE_KEY    — RSA private key (PEM string or path to .pem file)
"""
import base64
import logging
import os
import time
from datetime import UTC

from src.apis.base import get_json

logger = logging.getLogger(__name__)

_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Sports-relevant category tags Kalshi uses
_SPORT_TAGS = {
    # ── US Major ──────────────────────────────────────────────────────────────
    "americanfootball_nfl":           ["NFL", "FOOTBALL"],
    "americanfootball_ncaaf":         ["NCAAF", "COLLEGE FOOTBALL", "CFB"],
    "americanfootball_cfl":           ["CFL", "CANADIAN FOOTBALL"],
    "basketball_nba":                 ["NBA", "BASKETBALL"],
    "basketball_nba_summer_league":   ["NBA SUMMER", "SUMMER LEAGUE"],
    "basketball_wnba":                ["WNBA", "WOMEN'S BASKETBALL"],
    "basketball_ncaab":               ["NCAAB", "COLLEGE BASKETBALL"],
    "basketball_wncaab":              ["NCAAW", "WOMEN'S COLLEGE BASKETBALL"],
    "baseball_mlb":                   ["MLB", "BASEBALL"],
    "icehockey_nhl":                  ["NHL", "HOCKEY"],
    "icehockey_pwhl":                 ["PWHL", "WOMEN'S HOCKEY"],
    # ── Basketball — International ────────────────────────────────────────────
    "basketball_euroleague":          ["EUROLEAGUE", "EURO LEAGUE"],
    "basketball_eurocup":             ["EUROCUP"],
    "basketball_fiba_world_cup":      ["FIBA", "BASKETBALL WORLD CUP"],
    "basketball_nbl":                 ["NBL", "AUSTRALIAN BASKETBALL"],
    # ── Baseball — International ──────────────────────────────────────────────
    "baseball_npb":                   ["NPB", "JAPAN BASEBALL", "NIPPON PROFESSIONAL"],
    "baseball_kbo":                   ["KBO", "KOREA BASEBALL", "KOREAN BASEBALL"],
    # ── Soccer (Men's) ───────────────────────────────────────────────────────
    "soccer_epl":                     ["SOCCER", "EPL", "PREMIER LEAGUE", "ENGLISH PREMIER"],
    "soccer_usa_mls":                 ["MLS", "MAJOR LEAGUE SOCCER"],
    "soccer_usa_usl_championship":    ["USL"],
    "soccer_spain_la_liga":           ["LA LIGA", "LALIGA"],
    "soccer_germany_bundesliga":      ["BUNDESLIGA"],
    "soccer_italy_serie_a":           ["SERIE A"],
    "soccer_france_ligue_one":        ["LIGUE 1", "LIGUE ONE"],
    "soccer_netherlands_eredivisie":  ["EREDIVISIE"],
    "soccer_portugal_primeira_liga":  ["PRIMEIRA LIGA"],
    "soccer_turkey_super_league":     ["SUPER LIG", "TURKISH SUPER"],
    "soccer_fifa_world_cup":          ["WORLD CUP", "FIFA", "FOOTBALL"],
    "soccer_fifa_club_world_cup":     ["CLUB WORLD CUP", "CWC"],
    "soccer_uefa_champs_league":      ["CHAMPIONS LEAGUE", "UCL"],
    "soccer_uefa_europa_league":      ["EUROPA LEAGUE"],
    "soccer_conmebol_copa_libertadores": ["COPA LIBERTADORES"],
    "soccer_conmebol_copa_america":   ["COPA AMERICA"],
    "soccer_africa_cup_of_nations":   ["AFCON"],
    "soccer_brazil_campeonato":       ["BRASILEIRAO", "CAMPEONATO BRASILEIRO"],
    "soccer_argentina_primera_division": ["LIGA PROFESIONAL"],
    "soccer_mexico_ligamx":           ["LIGA MX"],
    "soccer_saudi_arabia_premier_league": ["SAUDI PRO LEAGUE", "ROSHN"],
    "soccer_japan_j_league":          ["J LEAGUE", "J-LEAGUE"],
    "soccer_south_korea_kleague1":    ["K LEAGUE", "K-LEAGUE"],
    "soccer_australia_aleague":       ["A-LEAGUE"],
    # ── Soccer (Women's) ─────────────────────────────────────────────────────
    "soccer_usa_nwsl":                ["NWSL", "NATIONAL WOMEN'S SOCCER"],
    "soccer_fifa_womens_world_cup":   ["WOMEN'S WORLD CUP", "WWC"],
    "soccer_england_wsl":             ["WSL", "WOMEN'S SUPER LEAGUE"],
    # ── Tennis (Men's & Women's) ─────────────────────────────────────────────
    "tennis_atp_wimbledon":           ["WIMBLEDON", "TENNIS"],
    "tennis_wta_wimbledon":           ["WIMBLEDON WOMEN", "WTA WIMBLEDON"],
    "tennis_atp_us_open":             ["US OPEN TENNIS"],
    "tennis_wta_us_open":             ["US OPEN WOMEN"],
    "tennis_atp_australian_open":     ["AUSTRALIAN OPEN"],
    "tennis_atp_french_open":         ["FRENCH OPEN", "ROLAND GARROS"],
    "tennis_atp_toronto":             ["ATP TORONTO", "CANADIAN OPEN"],
    "tennis_atp_cincinnati":          ["ATP CINCINNATI", "WESTERN SOUTHERN"],
    "tennis_davis_cup":               ["DAVIS CUP"],
    "tennis_billie_jean_king_cup":    ["BILLIE JEAN KING CUP"],
    # ── Golf (Men's & Women's) ───────────────────────────────────────────────
    "golf_pga_tour":                  ["GOLF", "PGA TOUR"],
    "golf_lpga":                      ["LPGA", "WOMEN'S GOLF"],
    "golf_masters_tournament_winner": ["MASTERS"],
    "golf_the_open_championship_winner": ["THE OPEN", "BRITISH OPEN"],
    "golf_dp_world_tour":             ["DP WORLD TOUR", "EUROPEAN TOUR"],
    # ── Combat Sports ────────────────────────────────────────────────────────
    "mma_mixed_martial_arts":         ["UFC", "MMA", "MIXED MARTIAL ARTS"],
    "boxing_boxing":                  ["BOXING", "BOX"],
    # ── Motorsport ───────────────────────────────────────────────────────────
    "motorsport_formula_1":           ["F1", "FORMULA 1", "FORMULA ONE", "GRAND PRIX"],
    "motorsport_nascar_cup_series":   ["NASCAR", "NASCAR CUP"],
    "motorsport_indycar":             ["INDYCAR", "INDY 500"],
    "motorsport_motogp":              ["MOTOGP", "MOTO GP"],
    # ── Cricket ───────────────────────────────────────────────────────────────
    "cricket_ipl":                    ["IPL", "INDIAN PREMIER LEAGUE"],
    "cricket_international_t20":      ["T20", "CRICKET T20"],
    "cricket_test_match":             ["TEST MATCH", "TEST CRICKET"],
    "cricket_odi":                    ["ODI", "ONE DAY INTERNATIONAL"],
    "cricket_bbl":                    ["BIG BASH", "BBL"],
    "cricket_psl":                    ["PSL", "PAKISTAN SUPER LEAGUE"],
    "cricket_cpl":                    ["CPL", "CARIBBEAN PREMIER"],
    "cricket_sa20":                   ["SA20"],
    "cricket_the_hundred":            ["THE HUNDRED"],
    # ── Aussie Rules (Men + Women) ────────────────────────────────────────────
    "aussierules_afl":                ["AFL", "AUSTRALIAN FOOTBALL"],
    "aussierules_aflw":               ["AFLW", "WOMEN'S AFL"],
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "rugbyleague_nrl":                ["NRL", "NATIONAL RUGBY LEAGUE", "RUGBY LEAGUE"],
    "rugbyleague_nrl_state_of_origin": ["STATE OF ORIGIN"],
    "rugbyleague_super_league":       ["SUPER LEAGUE", "RUGBY SUPER LEAGUE"],
    "rugbyunion_six_nations":         ["SIX NATIONS"],
    "rugbyunion_super_rugby":         ["SUPER RUGBY"],
    "rugbyunion_world_cup":           ["RUGBY WORLD CUP", "RWC"],
    # ── Darts ─────────────────────────────────────────────────────────────────
    "darts_pdc_world_championship":   ["DARTS", "PDC"],
    "darts_premier_league":           ["DARTS PREMIER LEAGUE"],
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker_world_championship":     ["SNOOKER"],
}


def _key_id() -> str:
    return os.getenv("KALSHI_API_KEY_ID", "").strip()


def _private_key():
    """Load RSA private key from env var (PEM string) or file path."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        return None

    raw = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
    if not raw:
        return None

    # May be a file path or inline PEM
    if os.path.isfile(raw):
        with open(raw, "rb") as f:
            pem = f.read()
    else:
        pem = raw.encode()

    try:
        return load_pem_private_key(pem, password=None)
    except Exception as e:
        logger.warning("Kalshi: could not load private key: %s", e)
        return None


def _sign_request(method: str, path: str) -> dict | None:
    """Build Kalshi auth headers for a request."""
    key_id = _key_id()
    private_key = _private_key()
    if not key_id or not private_key:
        return None

    try:
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes

        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = private_key.sign(msg, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY":       key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
    except Exception as e:
        logger.warning("Kalshi: request signing failed: %s", e)
        return None


def _get(path: str, params: dict | None = None) -> dict | list | None:
    headers = _sign_request("GET", path)
    if not headers:
        logger.warning("Kalshi: no auth headers — KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY may be missing; trying unauthenticated")
        headers = {}
    try:
        return get_json(f"{_BASE}{path}", params=params, headers=headers)
    except Exception as e:
        logger.warning("Kalshi GET %s failed: %s", path, e)
        return None


def get_markets(sport_key: str | None = None, limit: int = 200) -> list[dict]:
    """
    Fetch active event markets from Kalshi.
    Optionally filtered by sport_key — returns all sports markets if None.
    """
    params: dict = {"limit": limit, "status": "open"}

    data = _get("/markets", params)
    if not data:
        return []

    markets_raw = data.get("markets", []) if isinstance(data, dict) else []

    # Filter by sport tags if requested
    target_tags = set()
    if sport_key:
        for tag in _SPORT_TAGS.get(sport_key, []):
            target_tags.add(tag.upper())

    out = []
    for m in markets_raw:
        tags = [t.upper() for t in (m.get("tags") or [])]
        category = (m.get("category") or "").upper()

        if target_tags and not (target_tags & set(tags)) and category not in target_tags:
            continue

        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)
        no_bid  = float(m.get("no_bid_dollars")  or m.get("no_bid")  or 0)
        no_ask  = float(m.get("no_ask_dollars")  or m.get("no_ask")  or 0)
        last    = float(m.get("last_price_dollars") or m.get("last_price") or 0)
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid or last
        if yes_mid > 1: yes_mid /= 100
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid or (1 - yes_mid if yes_mid else 0)
        if no_mid  > 1: no_mid  /= 100

        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "category":     category,
            "tags":         tags,
            "yes_price":    round(yes_mid, 4),    # implied prob of YES (0-1)
            "no_price":     round(no_mid,  4),    # implied prob of NO  (0-1)
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "result":       m.get("result", ""),
            "sport_key":    sport_key or "",
            "source":       "kalshi",
        })

    return out


_SPORTS_KEYWORDS = [
    # ── US Major Leagues ──────────────────────────────────────────────────────
    "nba", "nfl", "mlb", "nhl", "ufc", "mma", "ncaa",
    "wnba", "nwsl", "pwhl", "lpga", "wta", "ncaaw",
    # ── Basketball (International) ────────────────────────────────────────────
    "summer league", "nba summer", "euroleague", "euro league", "eurocup",
    "fiba", "nbl", "liga acb", "lba basket", "bbl basketball", "vtb united",
    # ── Baseball (International) ──────────────────────────────────────────────
    "npb", "kbo", "cpbl", "nippon professional", "korean baseball",
    # ── Ice Hockey ────────────────────────────────────────────────────────────
    "khl", "shl hockey", "liiga", "ahl hockey", "iihf",
    # ── American / Canadian Football ─────────────────────────────────────────
    "cfl", "canadian football",
    # ── Soccer / Football ─────────────────────────────────────────────────────
    "champions league", "premier league", "mls", "liga", "serie a",
    "bundesliga", "ligue 1", "eredivisie", "primeira liga",
    "copa libertadores", "copa america", "afcon", "world cup",
    "super lig", "super liga", "primera division",
    "fifa", "uefa", "conmebol",
    "j league", "k league", "a-league", "a league",
    "brasileirao", "liga mx", "saudi pro",
    "nwsl", "wsl", "women's super league",
    "both teams to score", "btts", "clean sheet",
    # ── Tennis ────────────────────────────────────────────────────────────────
    "wimbledon", "us open", "french open", "australian open", "roland garros",
    "masters", "atp", "wta", "grand slam", "davis cup", "laver cup",
    "billie jean",
    # ── Golf ──────────────────────────────────────────────────────────────────
    "pga", "lpga", "the open", "british open", "dp world tour", "european tour",
    "birdies", "bogeys", "eagles", "make the cut",
    # ── Motorsport ────────────────────────────────────────────────────────────
    "formula 1", "formula one", "f1", "grand prix", "nascar",
    "indycar", "indy 500", "motogp", "moto gp",
    "fastest lap", "pole position", "laps led",
    # ── Cricket ────────────────────────────────────────────────────────────────
    "cricket", "ipl", "test match", "odi", "t20",
    "big bash", "bbl", "psl", "caribbean premier", "cpl",
    "sa20", "the hundred", "six nations cricket",
    # ── Australian Rules ──────────────────────────────────────────────────────
    "afl", "aflw", "australian football", "collingwood", "richmond",
    "geelong", "hawthorn", "essendon", "carlton", "melbourne",
    "sydney swans", "west coast", "port adelaide", "brisbane lions",
    "gold coast", "gws giants", "fremantle", "north melbourne",
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "nrl", "national rugby league", "rugby league", "super league",
    "super rugby", "six nations", "rugby world cup", "state of origin",
    "broncos", "roosters", "storm", "rabbitohs", "panthers",
    "eels", "sharks", "knights", "bulldogs", "titans",
    # ── Darts ─────────────────────────────────────────────────────────────────
    "darts", "pdc", "premier league darts", "world matchplay",
    "180", "checkout", "oche",
    # ── Snooker ───────────────────────────────────────────────────────────────
    "snooker", "century break", "maximum break", "frame",
    # ── MMA / Boxing ─────────────────────────────────────────────────────────
    "ufc", "boxing", "ko", "tko", "submission", "decision", "knockdown",
    # ── General game terms ────────────────────────────────────────────────────
    "game ", "match", "score", "innings", "quarter", "half",
    "total ", "over ", "under ", "spread",
    "goals", "goal", "runs", "sets", "aces",
    # ── US NBA / NFL / MLB / NHL teams ────────────────────────────────────────
    "heat", "celtics", "lakers", "warriors", "knicks", "nuggets", "bucks",
    "suns", "clippers", "nets", "sixers", "hawks", "bulls", "cavs",
    "cavaliers", "thunder", "jazz", "pelicans", "spurs", "rockets",
    "mavericks", "timberwolves", "blazers", "kings", "pacers",
    "magic", "pistons", "raptors", "hornets", "wizards", "grizzlies",
    "yankees", "dodgers", "mets", "red sox", "cubs", "astros", "braves",
    "orioles", "phillies", "cardinals", "giants", "padres", "angels",
    "mariners", "tigers", "twins", "pirates", "reds", "rockies",
    "oilers", "panthers", "rangers", "avalanche", "lightning",
    "bruins", "canadiens", "maple leafs", "sabres", "blackhawks",
    "penguins", "capitals", "flyers", "islanders", "blue jackets",
    # ── WNBA teams ────────────────────────────────────────────────────────────
    "fever", "liberty", "aces", "sun", "lynx", "sky", "storm", "mystics",
    "wings", "dream", "sparks", "mercury",
    # ── International soccer teams ────────────────────────────────────────────
    "real madrid", "barcelona", "manchester city", "manchester united",
    "liverpool", "arsenal", "chelsea", "tottenham", "inter milan",
    "ac milan", "juventus", "napoli", "psg", "paris saint-germain",
    "bayern munich", "dortmund", "atletico madrid",
    "england", "portugal", "brazil", "france", "germany", "spain",
    "argentina", "morocco", "senegal", "nigeria", "algeria", "colombia",
    "croatia", "netherlands", "italy", "mexico", "usa", "japan", "korea",
    "saudi arabia", "australia", "belgium", "denmark", "uruguay",
    # ── Player prop terms ────────────────────────────────────────────────────
    "player ", "will ", "score more than", "record ",
    "points", "rebounds", "assists", "blocks", "steals", "threes",
    "passing yards", "rushing yards", "receiving yards", "touchdowns",
    "hits", "home run", "strikeout", "rbi", "stolen base",
    "saves", "shots on goal", "goalkeeper",
    "aces", "double faults", "break points",
    "round ",
]

# Futures / politics patterns — always block (only long-term non-game markets)
_KALSHI_FUTURES = [
    "presidential", "election", "president",
    "primary", "governor", "senate", "congress", "bitcoin", "crypto",
    "before 20", "next year", "erupt before", "land on mars",
    "will humans colonize", "visit mars",
    "make the playoffs", "make playoffs", "win the championship",
    "win the title", "win the league", "win the serie",
]


def _kalshi_is_game_day(close_time: str) -> bool:
    """Return True if market closes within 36 hours (or has no close_time — assume live)."""
    if not close_time:
        return True  # no close_time = treat as current/live
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        return timedelta(minutes=-30) <= (dt - now) <= timedelta(hours=36)
    except Exception:
        return True  # unparseable = include rather than drop


def get_sports_markets() -> list[dict]:
    """
    Fetch active SINGLE-GAME sports markets from Kalshi (closes within 36 h).
    Excludes tournament futures and politics.
    """
    markets_raw: list[dict] = []
    for params in [
        {"limit": 200, "status": "open", "category": "Sports"},
        {"limit": 200, "status": "open"},
        {"limit": 200},
    ]:
        data = _get("/markets", params)
        if data:
            markets_raw = data.get("markets", []) if isinstance(data, dict) else []
            if markets_raw:
                break

    out = []
    for m in markets_raw:
        title      = (m.get("title") or "").lower()
        category   = (m.get("category") or "").lower()
        tags       = [t.lower() for t in (m.get("tags") or [])]
        close_time = m.get("close_time", "")

        # Block futures and politics regardless
        if any(pat in title for pat in _KALSHI_FUTURES):
            continue

        # Only single-game markets (ends within 48 h)
        if not _kalshi_is_game_day(close_time):
            continue

        is_sports = (
            "sports" in category
            or any(kw in title for kw in _SPORTS_KEYWORDS)
            or any(kw in " ".join(tags) for kw in _SPORTS_KEYWORDS)
        )
        if not is_sports:
            continue

        yes_bid = (m.get("yes_bid") or 0) / 100
        yes_ask = (m.get("yes_ask") or 0) / 100
        no_bid  = (m.get("no_bid")  or 0) / 100
        no_ask  = (m.get("no_ask")  or 0) / 100

        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid

        if not yes_mid and not no_mid:
            continue  # no valid price on either side — skip illiquid market

        # Detect sport from title
        sport_key = ""
        for sk, tag_list in _SPORT_TAGS.items():
            if any(t.lower() in title for t in tag_list):
                sport_key = sk
                break

        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "category":     category,
            "tags":         tags,
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "result":       m.get("result", ""),
            "sport_key":    sport_key,
            "source":       "kalshi",
        })

    logger.info("Kalshi: %d sports markets fetched (from %d total)", len(out), len(markets_raw))
    return out


def _prob_to_american(prob: float) -> int:
    """Convert implied probability (0-1) to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    return int(100 * (1 - prob) / prob)


def get_event_markets(event_ticker: str) -> list[dict]:
    """
    Fetch all sub-markets for a Kalshi event (player props, game props, spreads, totals).
    event_ticker is the event-level ticker e.g. 'FIFA-WCSF-FRAVEN-20260616'.
    """
    data = _get(f"/events/{event_ticker}")
    if not data:
        return []
    markets = (data.get("event") or {}).get("markets", []) or data.get("markets", [])
    out = []
    for m in markets:
        yes_bid = (m.get("yes_bid") or 0) / 100
        yes_ask = (m.get("yes_ask") or 0) / 100
        no_bid  = (m.get("no_bid")  or 0) / 100
        no_ask  = (m.get("no_ask")  or 0) / 100
        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid
        out.append({
            "market_id":    m.get("ticker", ""),
            "title":        m.get("title", ""),
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":       m.get("volume", 0),
            "close_time":   m.get("close_time", ""),
            "source":       "kalshi",
        })
    return out


def get_sports_events(limit: int = 500) -> list[dict]:
    """
    Fetch active sports markets from Kalshi.
    Queries known sports series tickers directly (most reliable) then falls
    back to paginated /markets scan. Covers MLB, WNBA, FIFA CWC, MLS, NFL, NBA, NHL, tennis.
    Returns flat list of markets sorted by volume.
    """
    # Known Kalshi sports series tickers — query each directly for best results
    _SERIES = [
        # ── Baseball (US) ─────────────────────────────────────────
        "KXMLBGAME",        # MLB game winners
        "KXMLBTOTAL",       # MLB run totals
        "KXMLBHITS",        # MLB player hits props
        "KXMLBHR",          # MLB home runs props
        "KXMLBRBI",         # MLB RBI props
        "KXMLBSO",          # MLB strikeouts props (pitcher)
        "KXMLBTEAM",        # MLB team props
        "KXMLBPITCHER",     # MLB pitcher props
        "KXMLBBATTER",      # MLB batter props
        # ── Baseball (International) ──────────────────────────────
        "KXKBOGAME",        # KBO (Korea) game winners
        "KXKBOTOTAL",       # KBO totals
        "KXNPBGAME",        # NPB (Japan) game winners
        "KXNPBTOTAL",       # NPB totals
        # ── Soccer (Men's) ────────────────────────────────────────
        "KXWCGAME",         # FIFA Club World Cup game winners
        "KXWCTOTAL",        # FIFA Club World Cup goal totals
        "KXWCGOAL",         # FIFA CWC goalscorer props
        "KXWCTEAM",         # FIFA CWC team props
        "KXWCBTTS",         # FIFA CWC both teams to score
        "KXMLSGAME",        # MLS game winners
        "KXMLSTOTAL",       # MLS totals
        "KXMLSGOAL",        # MLS goalscorer props
        "KXMLSBTTS",        # MLS both teams to score
        "KXEPLGAME",        # English Premier League
        "KXEPLTOTAL",       # EPL totals
        "KXEPLGOAL",        # EPL goalscorer props
        "KXEPLBTTS",        # EPL both teams to score
        "KXUEFAGAME",       # UEFA Champions League
        "KXUEFATOTAL",      # UEFA totals
        "KXUEFAGOAL",       # UEFA goalscorer props
        "KXUEFABTTS",       # UEFA BTTS
        "KXEUROPAGAME",     # UEFA Europa League
        "KXLALIGAGAME",     # La Liga
        "KXLALIGABTTS",     # La Liga BTTS
        "KXBUNDESLIGAGAME", # Bundesliga
        "KXSERIEAGAME",     # Serie A
        "KXLIGUE1GAME",     # Ligue 1
        "KXBRAZILGAME",     # Brasileirao
        "KXARGENTINAGAME",  # Liga Profesional Argentina
        "KXLIGAMXGAME",     # Liga MX
        "KXJLEAGUEGAME",    # J League
        "KXKLEAGUEGAME",    # K League
        "KXSAUDIGAME",      # Saudi Pro League
        "KXCL2025GAME",     # Champions League 2024-25
        # ── Soccer (Women's) ──────────────────────────────────────
        "KXNWSLGAME",       # NWSL game winners
        "KXNWSLTOTAL",      # NWSL totals
        "KXNWSLGOAL",       # NWSL goalscorer props
        "KXWSLGAME",        # WSL (England Women's)
        "KXWWCGAME",        # FIFA Women's World Cup
        "KXWWCTOTAL",       # FIFA Women's WC totals
        # ── Basketball (Men's) ────────────────────────────────────
        "KXNBAGAME",        # NBA game winners
        "KXNBATOTAL",       # NBA totals
        "KXNBAPOINTS",      # NBA player points props
        "KXNBAREBOUNDS",    # NBA player rebounds props
        "KXNBAASSISTS",     # NBA player assists props
        "KXNBATEAM",        # NBA team props
        "KXNBATHREES",      # NBA three-pointers props
        "KXNBABLOCKS",      # NBA blocks/steals props
        "KXNCAABGAME",      # NCAA Men's Basketball
        # ── Basketball — Summer League ────────────────────────────
        "KXNBASUMMERGAME",  # NBA Summer League game winners
        "KXNBASUMMERTOTAL", # NBA Summer League totals
        "KXSUMMERGAME",     # Summer League (alt ticker)
        "KXSUMMERLEAGUE",   # Summer League (alt ticker)
        # ── Basketball — International ────────────────────────────
        "KXEUROLBGAME",     # EuroLeague game winners
        "KXEUROLBTOTAL",    # EuroLeague totals
        "KXFIBAGAME",       # FIBA games
        "KXNBLGAME",        # NBL (Australia)
        # ── Basketball (Women's) ──────────────────────────────────
        "KXWNBAGAME",       # WNBA game winners
        "KXWNBATOTAL",      # WNBA totals
        "KXWNBAPOINTS",     # WNBA player points props
        "KXWNBAREBOUNDS",   # WNBA player rebounds props
        "KXWNBAASSISTS",    # WNBA player assists props
        "KXWNBATEAM",       # WNBA team props
        "KXNCAAWGAME",      # NCAA Women's Basketball
        # ── American Football ─────────────────────────────────────
        "KXNFLGAME",        # NFL game winners
        "KXNFLTOTAL",       # NFL totals
        "KXNFLPASS",        # NFL passing yards props
        "KXNFLRUSH",        # NFL rushing yards props
        "KXNFLREC",         # NFL receiving yards props
        "KXNFLTD",          # NFL touchdown props
        "KXNFLTEAM",        # NFL team props
        "KXNCAAFGAME",      # College Football
        "KXCFLGAME",        # CFL (Canada)
        "KXCFLTOTAL",       # CFL totals
        # ── Hockey ────────────────────────────────────────────────
        "KXNHLGAME",        # NHL game winners
        "KXNHLTOTAL",       # NHL totals
        "KXNHLGOAL",        # NHL goalscorer props
        "KXNHLSAVES",       # NHL goalie saves props
        "KXNHLTEAM",        # NHL team props
        "KXPWHLGAME",       # PWHL (women's hockey)
        "KXPWHLTOTAL",      # PWHL totals
        # ── Tennis (Men's & Women's) ──────────────────────────────
        "KXTENNIS",         # Tennis general
        "KXATPGAME",        # ATP (men's tennis)
        "KXATPSETS",        # ATP sets props
        "KXWTGAME",         # WTA (women's tennis)
        "KXWTSETS",         # WTA sets props
        "KXWIMBLEDON",      # Wimbledon
        "KXATPWIMBLEDON",   # ATP Wimbledon
        "KXWTAWIMBLEDON",   # WTA Wimbledon
        "KXUSOPEN",         # US Open Tennis
        "KXAUSOPEN",        # Australian Open
        "KXROLGARROS",      # French Open / Roland Garros
        "KXDAVISCUP",       # Davis Cup
        # ── Golf (Men's & Women's) ────────────────────────────────
        "KXGOLF",           # Golf general
        "KXPGAGAME",        # PGA Tour
        "KXPGACUTS",        # PGA cut props
        "KXPGAWIN",         # PGA winner outright
        "KXLPGAGAME",       # LPGA Tour (women's golf)
        "KXLPGACUTS",       # LPGA cut props
        "KXMASTERS",        # The Masters
        "KXTHEOPEN",        # The Open Championship
        "KXDPWORLDTOUR",    # DP World Tour (European Tour)
        # ── Combat Sports ─────────────────────────────────────────
        "KXUFC",            # UFC/MMA
        "KXUFCMETHOD",      # UFC method of victory props
        "KXUFCROUND",       # UFC round props
        "KXBOXGAME",        # Boxing
        "KXBOXMETHOD",      # Boxing method of victory props
        # ── Racing ────────────────────────────────────────────────
        "KXF1GAME",         # Formula 1
        "KXF1DRIVER",       # F1 driver props
        "KXF1RACE",         # F1 race winner
        "KXF1FASTEST",      # F1 fastest lap
        "KXNASCARGAME",     # NASCAR
        "KXNASCARDRIVER",   # NASCAR driver props
        "KXINDYCARGAME",    # IndyCar
        "KXMOTOGPGAME",     # MotoGP
        # ── Cricket ───────────────────────────────────────────────
        "KXCRICKET",        # Cricket general
        "KXCRICKETGAME",    # Cricket match winner
        "KXIPLTOTAL",       # IPL totals
        "KXIPLGAME",        # IPL match winner
        "KXTEST",           # Test match cricket
        "KXODIMATCH",       # ODI match
        "KXBBLGAME",        # Big Bash League
        "KXPSLGAME",        # Pakistan Super League
        "KXCPLGAME",        # Caribbean Premier League
        "KXSA20GAME",       # SA20 South Africa
        "KXHUNDREDGAME",    # The Hundred
        # ── Australian Football (AFL) ──────────────────────────────
        "KXAFLGAME",        # AFL match winner
        "KXAFLTOTAL",       # AFL totals
        "KXAFLTIPS",        # AFL tips
        "KXAFLWGAME",       # AFLW (women's)
        # ── Rugby League ──────────────────────────────────────────
        "KXNRLGAME",        # NRL match winner
        "KXNRLTOTAL",       # NRL totals
        "KXNRLTIPS",        # NRL tips
        "KXSUPERLEAGUEGAME",# Super League (UK)
        "KXSOGAME",         # State of Origin
        # ── Rugby Union ───────────────────────────────────────────
        "KXSIXNATIONSGAME", # Six Nations
        "KXRWCGAME",        # Rugby World Cup
        "KXSUPERRUGBYGAME", # Super Rugby
        # ── Darts ─────────────────────────────────────────────────
        "KXDARTSGAME",      # Darts match winner
        "KXDARTSPREMIER",   # Darts Premier League
        "KXPDCGAME",        # PDC event
        # ── Snooker ───────────────────────────────────────────────
        "KXSNOOKERGAME",    # Snooker match
    ]

    # Return cached result if fresh (40-min TTL — Kalshi markets don't change every second)
    try:
        import json as _json
        from src.core.config import REDIS_URL as _REDIS_URL
        import redis as _redis_mod
        _rc = _redis_mod.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        _cached = _rc.get("kalshi:series_raw")
        if _cached:
            _raw_list = _json.loads(_cached)
            logger.debug("Kalshi get_sports_events: returning %d markets from cache", len(_raw_list))
            # still run the filter/format section below by injecting into all_markets
            all_markets_cached: list[dict] = _raw_list
        else:
            all_markets_cached = []
    except Exception:
        _rc = None
        all_markets_cached = []

    if all_markets_cached:
        all_markets = all_markets_cached
        seen_tickers: set[str] = {m.get("ticker", "") for m in all_markets}
    else:
        all_markets = []
        seen_tickers = set()

        # 1. Direct series queries — most reliable, hits the right markets immediately
        for i, series in enumerate(_SERIES):
            data = _get("/markets", {"limit": 100, "status": "open", "series_ticker": series})
            if data and isinstance(data, dict):
                for m in data.get("markets", []):
                    t = m.get("ticker", "")
                    if t and t not in seen_tickers:
                        seen_tickers.add(t)
                        all_markets.append(m)
            if i % 10 == 9:
                time.sleep(0.3)  # brief pause every 10 requests to avoid burst 429s

        if not all_markets:
            logger.warning("Kalshi series fetch returned 0 markets across %d series — possible auth failure or API outage", len(_SERIES))
        else:
            logger.info("Kalshi series fetch: %d markets from %d series", len(all_markets), len(_SERIES))

        # 2. Fallback: paginated scan catches anything not in known series
        if len(all_markets) < 50:
            cursor = None
            for _ in range(5):
                params: dict = {"limit": 100, "status": "open"}
                if cursor:
                    params["cursor"] = cursor
                data = _get("/markets", params)
                if not data or not isinstance(data, dict):
                    break
                for m in data.get("markets", []):
                    t = m.get("ticker", "")
                    if t and t not in seen_tickers:
                        seen_tickers.add(t)
                        all_markets.append(m)
                cursor = data.get("cursor")
                if not cursor:
                    break

        # Cache raw markets for 40 min so subsequent calls skip the 80-request series loop
        try:
            import json as _json2
            if _rc is not None and all_markets:
                _rc.setex("kalshi:series_raw", 2400, _json2.dumps(all_markets))
        except Exception:
            pass

    logger.info("Kalshi /markets: %d total fetched", len(all_markets))

    out = []
    for m in all_markets:
        title = (m.get("title") or "").lower()

        # Sports keyword filter
        is_sports = (
            any(kw in title for kw in _SPORTS_KEYWORDS)
            or (m.get("category") or "").lower() == "sports"
        )
        if not is_sports:
            continue

        # Block long-term futures
        if any(pat in title for pat in _KALSHI_FUTURES):
            continue

        # Filter by expected_expiration_time — active window: 5 AM to 3 AM ET
        # Include games from 3h ago through next 3 AM ET cutoff
        exp_raw = m.get("expected_expiration_time") or m.get("expiration_time") or ""
        if exp_raw:
            try:
                from datetime import datetime as _dt, timedelta as _td
                import zoneinfo as _zi
                _ET      = _zi.ZoneInfo("America/New_York")
                _exp_utc = _dt.fromisoformat(exp_raw.replace("Z", "+00:00"))
                _now_utc = _dt.now(UTC)
                _now_et  = _now_utc.astimezone(_ET)
                # Next 6 AM ET cutoff — wide enough to include late-night games
                _cutoff_et = _now_et.replace(hour=6, minute=0, second=0, microsecond=0)
                if _cutoff_et <= _now_et:
                    _cutoff_et = _cutoff_et + _td(days=1)  # tomorrow 6 AM
                _cutoff_utc = _cutoff_et.astimezone(UTC)
                if _exp_utc < _now_utc - _td(hours=3):
                    continue  # game ended more than 3h ago
                if _exp_utc > _cutoff_utc:
                    continue  # past tonight's cutoff
            except Exception:
                pass  # include if unparseable

        yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_bid") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or m.get("yes_ask") or 0)
        no_bid  = float(m.get("no_bid_dollars")  or m.get("no_bid")  or 0)
        no_ask  = float(m.get("no_ask_dollars")  or m.get("no_ask")  or 0)
        last    = float(m.get("last_price_dollars") or m.get("last_price") or 0)

        yes_mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid or last

        # Normalise to 0-1 BEFORE computing no_mid fallback
        if yes_mid > 1:
            yes_mid /= 100
        no_mid  = (no_bid  + no_ask)  / 2 if no_bid  and no_ask  else no_ask  or no_bid  or (1 - yes_mid if yes_mid else 0)
        if no_mid > 1:
            no_mid /= 100

        if not yes_mid:
            continue

        vol = float(m.get("volume_24h_fp") or m.get("volume_fp") or m.get("volume") or 0)

        subtitle = (m.get("subtitle") or "").strip()

        # Convert game_time and close_time from UTC → ET ISO string
        def _to_et(raw: str) -> str:
            if not raw:
                return ""
            try:
                from datetime import datetime as _dt2
                import zoneinfo as _zi2
                _ET2 = _zi2.ZoneInfo("America/New_York")
                _dt_utc = _dt2.fromisoformat(raw.replace("Z", "+00:00"))
                if _dt_utc.tzinfo is None:
                    _dt_utc = _dt_utc.replace(tzinfo=UTC)
                return _dt_utc.astimezone(_ET2).isoformat()
            except Exception:
                return raw

        _expiration_raw = m.get("expected_expiration_time") or m.get("expiration_time") or ""
        _close_time_raw = m.get("close_time", "")

        out.append({
            "market_id":    m.get("ticker", ""),
            "event_ticker": m.get("event_ticker", ""),
            # subtitle = "Team A vs Team B" on most Kalshi sports markets
            "event_title":  subtitle or m.get("title", ""),
            "title":        m.get("title", ""),
            "subtitle":     subtitle,
            "category":     (m.get("category") or "").lower(),
            "tags":         [t.lower() for t in (m.get("tags") or [])],
            "yes_price":    round(yes_mid, 4),
            "no_price":     round(no_mid,  4),
            "yes_american": _prob_to_american(yes_mid),
            "no_american":  _prob_to_american(no_mid),
            "volume":            vol,
            # close_time = when Kalshi stops accepting bets = actual game start time
            # expected_expiration_time = when market settles (~2-3h AFTER game ends) — kept for filtering only
            "close_time":        _close_time_raw,
            "game_time":         _to_et(_close_time_raw),
            "expiration_time":   _to_et(_expiration_raw),
            "source":            "kalshi",
        })

    out.sort(key=lambda x: x["volume"], reverse=True)
    logger.info("Kalshi: %d sports markets fetched (from %d total)", len(out), len(all_markets))

    # Build grouped structure: event_ticker → {title, game_time, series_ticker, markets[]}
    # Stored in kalshi:events_grouped so UI/debug can show series > event > sub-markets.
    try:
        import json as _jg
        _grouped: dict[str, dict] = {}
        for _sm in out:
            _etk = _sm.get("event_ticker") or _sm.get("market_id", "")
            # Derive series ticker from event ticker (everything before the date segment)
            _parts = _etk.split("-")
            _series_tk = _parts[0] if _parts else _etk
            if _etk not in _grouped:
                _grouped[_etk] = {
                    "event_ticker":  _etk,
                    "series_ticker": _series_tk,
                    "title":         _sm.get("subtitle") or _sm.get("event_title", ""),
                    "game_time":     _sm.get("game_time", ""),
                    "close_time":    _sm.get("close_time", ""),
                    "markets":       [],
                }
            _grouped[_etk]["markets"].append({
                "market_id":   _sm["market_id"],
                "title":       _sm.get("title", ""),
                "yes_price":   _sm["yes_price"],
                "no_price":    _sm["no_price"],
                "yes_american": _sm["yes_american"],
                "no_american":  _sm["no_american"],
                "volume":      _sm["volume"],
            })
        if _rc:
            _rc.setex("kalshi:events_grouped", 2400, _jg.dumps(list(_grouped.values())))
            logger.debug("Kalshi: cached %d events in kalshi:events_grouped", len(_grouped))
    except Exception as _eg_err:
        logger.debug("Kalshi: events_grouped cache failed: %s", _eg_err)

    return out
