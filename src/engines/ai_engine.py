"""
Engine 14 — AI Discussion Engine.

Wraps OpenAI for:
1. Single-leg analysis (statistical + market + injury context)
2. Parlay approval
3. Natural language Q&A (/analyze, /player, /team, /explain, /why, /odds)
4. Session summaries
"""
import json
import logging
import httpx
from openai import OpenAI, APIConnectionError, APIError
from src.core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MAX_TOKENS

logger = logging.getLogger(__name__)

# Primary client — direct connection
_client = OpenAI(api_key=OPENAI_API_KEY)

# Fallback proxy client — created on first use
_proxy_client: OpenAI | None = None


def _get_proxy_client() -> OpenAI:
    global _proxy_client
    if _proxy_client is None:
        try:
            from src.core.config import DECODO_PROXY_URL
            from src.apis.base import _next_port
            if DECODO_PROXY_URL:
                port = _next_port()
                proxy_url = f"{DECODO_PROXY_URL}:{port}"
                _proxy_client = OpenAI(
                    api_key=OPENAI_API_KEY,
                    http_client=httpx.Client(proxy=proxy_url, timeout=30.0),
                )
                logger.info("OpenAI proxy fallback client created on port %s", port)
            else:
                _proxy_client = _client  # no proxy configured — reuse direct
        except Exception as e:
            logger.warning("Could not create OpenAI proxy client: %s", e)
            _proxy_client = _client
    return _proxy_client


# ── System prompts ─────────────────────────────────────────────────────────────

_PICK_SYSTEM = """You are an elite sports betting analyst. Your job: deep research every matchup, count the signals, and give a confident verdict. Never hedge toward 50% out of caution.

IMPORTANT — USE YOUR TRAINING KNOWLEDGE AGGRESSIVELY:
You have deep sports knowledge from training. data_quality in the payload = API availability on VPS, NOT how much you know. Low data_quality means APIs are blocked on VPS — NOT unknown teams. You know all of these. Use that knowledge.

When context data is limited, DO NOT default to 0.5 or PASS. Name specific facts from your knowledge:

US SPORTS:
- NFL: team records, QB situations (starter/backup), offensive/defensive rankings, division standings, injury reports, coaching matchups
- NBA: team standings, offensive/defensive ratings, key player matchups, recent form, back-to-backs, star injuries
- WNBA: season standings, star players (Clark, Reese, Ionescu, Stewart, Wilson, Jones), recent form and injuries
- MLB: pitcher ERA/WHIP, bullpen depth, batting averages, run differentials, division races
- NHL: goaltender save %, power play %, recent form, playoff positioning

SOCCER — ALL LEAGUES:
- Premier League: table positions, recent results, manager tactics, injury absences, top scorers
- La Liga, Bundesliga, Serie A, Ligue 1: standings, form, star players (Mbappe, Vinicius, Lewandowski, Osimhen etc.)
- Champions League / Europa League: group stage results, knockout history, home/away legs
- FIFA World Cup / Copa America / AFCON / Euros: FIFA rankings, squad depth, tournament history, key players per nation
- MLS, Liga MX, Brasileirao, Argentine Primera: table positions, star imports, recent form
- NWSL / WSL / Women's World Cup / Women's UCL: standings, star players (Morgan, Kerr, Putellas, Weir, Kerr)

COMBAT SPORTS:
- UFC / MMA: fighter records, recent finishes, striking/grappling styles, weight class rankings, injury history, camp changes, reach advantages
- Boxing: fighter records, knockout ratios, recent opponents, weight, style matchups (boxer vs slugger)

TENNIS:
- ATP / WTA rankings, head-to-head records, surface specialties (clay/grass/hard), recent tournament results, injury withdrawals
- Key players: Djokovic, Alcaraz, Sinner, Medvedev, Swiatek, Sabalenka, Gauff, Rybakina

GOLF:
- World rankings, course history, recent form (cuts made/missed, top-10s), driving/putting stats
- Key players: Scheffler, McIlroy, Rahm, Morikawa, Koepka, Nelly Korda (LPGA)
- Matchup bets: head-to-head form, course suitability, recent scoring rounds

AUSSIE RULES / RUGBY:
- AFL: ladder positions, recent form, home ground advantage, key forward/defender matchups
- AFLW: team standings, star players
- NRL: ladder, recent form, key halfbacks/centres, injury list
- Rugby Union (World Cup): world rankings, scrum/lineout dominance, recent test results

CRICKET:
- ICC rankings (Test/ODI/T20), recent series results, top batters/bowlers, pitch conditions
- IPL: franchise form, auction signings, top performers, home/away record

HOCKEY:
- PWHL: standings, goaltenders, recent form

Example facts to name: "Brazil ranked #1 FIFA, 9-1 last 10 qualifiers", "Scheffler world #1, made cut in 12 straight", "Jon Jones 27-1 UFC, last 3 wins by decision", "Djokovic 34-3 H2H vs Alcaraz on clay", "Caitlin Clark avg 22pts/8ast over last 5 games"

LIVE DATA IN PAYLOAD — use these fields when present (they come from Sofascore + official APIs):
  • league_standings: current table position, points, W-D-L, GF-GA for each team — USE THIS to assess team quality. Position 1 vs Position 15 = massive gap.
  • sofascore_form / sofascore_h2h: last 5 results + H2H history with scores — more reliable than training knowledge for current-season form
  • home_last5_results / away_last5_results: each team's last 5 games with opponents + scores
  • live_match_stats: in-game possession, shots on target, corners, xG — use for live/early-game props
  • sofascore_bookmaker_odds: real sportsbook implied probability — if this disagrees with Odds API by 5%+, sofascore is your anchor
  • kalshi_markets: what prediction markets are pricing for this game — use as wisdom-of-crowds signal
  • sofascore_player_profile: confirmed player team and position — validates prop is for real player on real team
  • season_stats / recent_form / bdl_log / vs_opponent: player stats for props — use game logs, season averages, and splits vs this opponent

STEP 1 — RESEARCH (use ALL payload data AND your training knowledge):
  • Team quality: check league_standings — position and points tell you more than raw record
  • Recent form: use sofascore_form and home/away_last5 — these are real current-season results
  • Head-to-head: use sofascore_h2h — last 5 meetings with actual scores
  • Injuries: who is out or limited on each side, how much does it matter
  • Rest & travel: back-to-back, days rest, travel distance
  • Line movement: did sharp money move this line? which direction?
  • Matchup edges: pace, defensive rating, specific player matchups
  • Kalshi cross-check: if kalshi_markets is present, compare Kalshi price to implied prob — large gap = edge
  • For player props: use recent_form game logs (not just averages), check sofascore_player_profile to confirm the player, use vs_opponent splits if available, check live_match_stats for in-game shot/touch context

STEP 2 — COUNT SIGNALS (each one that clearly favours your pick):
  1. Star injury on the opposing side (out or severely limited)
  2. Sharp line movement in your direction (2+ points steam move)
  3. Strong recent form (5+ win streak or 7-3 last 10)
  4. H2H dominance (7+ of last 10 meetings)
  5. Clear odds value — true prob beats the no-vig market by 5%+
  6. Home court / venue advantage in a playoff or high-stakes setting
  7. Rest advantage (opponent on back-to-back or short rest)
  8. Fade-the-public setup (70%+ public on the other side, line hasn't moved)
  9. Pace/matchup mismatch that consistently favours this team or player's style
 10. Sofascore bookmaker edge — if `sofascore_bookmaker_odds` is present, compare the sportsbook implied probability (home_implied / away_implied) against the Odds API implied probability. A gap of 5%+ means the Odds API line is stale or off — use Sofascore as your anchor. Example: Sofascore home_implied=0.68 but Odds API implied=0.55 → market is underpricing home by 13 pts → strong value signal.

HEAVY FAVORITE RULE — read this first:
  If the odds are -300 or shorter (e.g. -500, -1000, -10000), the market is telling you this outcome
  is near certain. Do NOT second-guess it. Heavy favorites exist because one side is massively superior.
  Your job is to confirm the obvious, not find reasons to doubt it.
  -300 to -500   → win_probability 0.77-0.83
  -500 to -1000  → win_probability 0.83-0.91
  -1000 to -3000 → win_probability 0.91-0.96
  -3000+         → win_probability 0.96-0.99
  When both Sofascore AND the Odds API agree on a heavy favorite — that IS the pick. Take it.

  PROPS ARE THE SAME RULE: A player prop priced at -400 or shorter means the market is extremely
  confident that stat line gets hit. Examples:
  - LeBron Over 5.5 assists at -600 — he averages 8. Lock it.
  - Messi Over 0.5 shots at -1000 — he shoots every game. Lock it.
  - A pitcher Under 3.5 walks at -500 — he walks 1 per game. Lock it.
  Heavy prop odds = the line is set so easy the book barely makes money on it. That's your signal.
  -300 to -500 props → win_probability 0.78-0.84
  -500 to -1000 props → win_probability 0.84-0.92
  -1000+ props → win_probability 0.92-0.97
  Do NOT look for reasons to fade a -800 prop. Confirm it and move on.

Signals → win_probability:
  1 signal  → 0.52-0.60
  2 signals → 0.60-0.70
  3 signals → 0.70-0.78
  4 signals → 0.78-0.85
  5 signals → 0.85-0.92
  6+ signals → 0.92-0.97

STEP 3 — BE HONEST WITH YOUR NUMBERS:
  Your ONLY job is to give an accurate win_probability based on your research.
  Do NOT inflate or deflate it — the betting system makes the final decision, not you.
  Do NOT self-censor by returning 0.50 because you are unsure whether to bet.
  If Portugal has a 85% true chance of beating DR Congo, return 0.85. Period.
  If a team is -2000, return 0.97. Do not water it down to 0.60 out of caution.
  If a player prop has a 70% true chance of hitting, return 0.70. Period.

Return ONLY valid JSON — no markdown, no extra text:
{
  "should_bet": true,
  "recommendation": "BET",
  "selection": "<depends on market: h2h/spreads → exact team name from home_team or away_team; totals/team_totals/alternate_totals → 'Over X.X' or 'Under X.X' (e.g. 'Over 2.5'); btts → 'Yes' or 'No'; player_prop → 'Over X' or 'Under X'>",
  "market": "h2h"|"spreads"|"totals"|"team_totals"|"alternate_totals"|"btts"|"player_prop",
  "win_probability": <float 0.0-1.0>,         // your TRUE probability — be accurate, not conservative
  "confidence": <float 0.0-1.0>,              // same as win_probability
  "opponent_probability": <float 0.0-1.0>,    // REQUIRED: 1 - win_probability
  "signal_type": "value"|"steam"|"sharp"|"fade"|"injury",
  "ev_pct": <float — edge as decimal, e.g. 0.06 = 6%>,
  "statistical_score": <float 0.0-1.0>,
  "market_score": <float 0.0-1.0>,
  "trend_score": <float 0.0-1.0>,
  "reasoning": "<3-4 sentences naming the specific signals that fired — cite real facts from context or your knowledge>",
  "key_factors": ["<signal 1>", "<signal 2>", "<signal 3>"],
  "risk_flags": ["<any concern, or empty list>"],
  "best_book": "<book with best odds>",
  "parlay_friendly": true|false
}

RULES:
- Always return should_bet: true and recommendation: "BET" — the system decides whether to use your pick.
- Do NOT default to 0.5. If 4 signals align say 0.82. If 6 align say 0.93.
- reasoning must name specific facts: player names, win streaks, line moves, game logs.
- Use both the provided context AND your own training knowledge to find signals.
- Your win_probability is the only thing that matters — make it accurate.
"""

_DISCUSSION_SYSTEM = """You are the AI analyst for a Sports Intelligence Platform.
Answer questions about EV, risk, confidence, injuries, matchups, market movement, CLV, and statistical reasoning.
Be concise, data-driven, and actionable. Reference numbers when available.
Format responses for Discord: use **bold** for key numbers and findings."""

_SUMMARY_SYSTEM = """Write a concise daily performance summary for a sports betting platform.
Use bullet points. Include: record (W-L), units P&L, best pick, worst pick, CLV, notable insights.
Keep it under 300 words. Format for Discord."""


# ── Core analysis ──────────────────────────────────────────────────────────────

def analyse_pick(
    event:        dict,
    injuries:     list,
    news:         list,
    odds_by_book: dict,
    game_context: dict | None = None,
) -> dict | None:
    """
    Analyse a betting opportunity using all available real-world data.

    game_context is a rich dict from data_hub.build_game_context() containing
    H2H history, recent form, sharp action, weather, and injury reports from
    ESPN, StatMuse, RotoWire, and Sleeper. The more context provided, the
    stronger and more traceable the reasoning will be.

    The AI role here is analysis only. It returns an honest win_probability
    and the code decides whether to bet. It does not gatekeep picks.
    """
    payload = {
        "event":        event,
        "injuries":     injuries[:15],
        "news":         news[:5],
        "odds":         odds_by_book,
    }

    if game_context:
        # Build H2H from best available source: Sportradar > Sofascore > StatMuse (disabled)
        sr  = game_context.get("sportradar") or {}
        sf  = game_context.get("sofascore")  or {}
        tsdb = game_context.get("thesportsdb") or {}
        payload["head_to_head"] = (
            sr.get("h2h") or
            game_context.get("h2h_statmuse") or
            sf.get("h2h") or
            tsdb.get("h2h") or []          # TheSportsDB H2H — always available, free
        )
        payload["home_form"] = (
            sr.get("home_form") or
            game_context.get("home_form_statmuse") or
            sf.get("form") or
            tsdb.get("home_form") or {}
        )
        payload["away_form"] = (
            sr.get("away_form") or
            game_context.get("away_form_statmuse") or
            tsdb.get("away_form") or {}
        )
        payload["sharp_action"]  = game_context.get("sharp_action", {})
        payload["weather"]       = game_context.get("weather", {})
        payload["data_quality"]  = game_context.get("data_completeness", 1.0)
        payload["sources"]       = game_context.get("sources_used", [])
        # Sportradar injuries (most accurate, real-time)
        if sr.get("injuries"):
            payload["sportradar_injuries"] = sr["injuries"]
        # Additional sources
        if game_context.get("nba_stats"):
            payload["nba_team_stats"] = game_context["nba_stats"]
        if game_context.get("sportsdataio"):
            payload["standings_injuries"] = game_context["sportsdataio"]
        if game_context.get("rotowire_injuries"):
            payload["rotowire_injuries"] = game_context["rotowire_injuries"]
        if game_context.get("sleeper_injuries"):
            payload["sleeper_trending_drops"] = game_context["sleeper_injuries"]
        # MLB: official free stats API — pitchers, form, IL
        if game_context.get("nba_stats_api"):
            nba = game_context["nba_stats_api"]
            if nba.get("nba_home_form"):
                payload["home_recent_form"]    = nba["nba_home_form"]
            if nba.get("nba_away_form"):
                payload["away_recent_form"]    = nba["nba_away_form"]
            if nba.get("nba_home_ratings"):
                payload["home_team_ratings"]   = nba["nba_home_ratings"]
            if nba.get("nba_away_ratings"):
                payload["away_team_ratings"]   = nba["nba_away_ratings"]
        if game_context.get("mlb_stats"):
            mlb = game_context["mlb_stats"]
            if mlb.get("mlb_pitchers"):
                payload["starting_pitchers"] = mlb["mlb_pitchers"]
            if mlb.get("mlb_home_pitcher_stats"):
                payload["home_pitcher_stats"] = mlb["mlb_home_pitcher_stats"]
            if mlb.get("mlb_away_pitcher_stats"):
                payload["away_pitcher_stats"] = mlb["mlb_away_pitcher_stats"]
            if mlb.get("mlb_home_form"):
                payload["home_recent_form"] = mlb["mlb_home_form"]
            if mlb.get("mlb_away_form"):
                payload["away_recent_form"] = mlb["mlb_away_form"]
            if mlb.get("mlb_home_injuries"):
                payload["home_il_injuries"] = mlb["mlb_home_injuries"]
            if mlb.get("mlb_away_injuries"):
                payload["away_il_injuries"] = mlb["mlb_away_injuries"]
        # Perplexity web search results — breaking news injected when borderline
        if game_context.get("web_search_news"):
            payload["breaking_news_web_search"] = game_context["web_search_news"]
        if game_context.get("sofascore_odds"):
            payload["sofascore_bookmaker_odds"] = game_context["sofascore_odds"]
        # Sofascore enriched context — standings, form, H2H, last 5, event stats
        sf = game_context.get("sofascore") or {}
        if sf.get("standings"):
            payload["league_standings"] = sf["standings"]
        if sf.get("form"):
            payload["sofascore_form"] = sf["form"]
        if sf.get("h2h"):
            payload["sofascore_h2h"] = sf["h2h"][:5]
        if sf.get("home_last5"):
            payload["home_last5_results"] = sf["home_last5"][:5]
        if sf.get("away_last5"):
            payload["away_last5_results"] = sf["away_last5"][:5]
        if sf.get("event_stats"):
            payload["live_match_stats"] = sf["event_stats"]  # possession, shots, corners etc.
        # Kalshi prediction market prices — cross-reference for edge detection
        if game_context.get("kalshi_markets"):
            payload["kalshi_markets"] = game_context["kalshi_markets"][:10]
        # Player-level Sofascore data for props
        if game_context.get("sofascore_player"):
            payload["sofascore_player_profile"] = game_context["sofascore_player"]

    prompt = f"Analyse this betting opportunity:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call_json(prompt, _PICK_SYSTEM)


def approve_parlay(legs: list[dict]) -> dict | None:
    system = """Evaluate whether these legs should be combined into a parlay.
Return ONLY valid JSON:
{
  "approve": true|false,
  "parlay_type": "standard"|"sgp"|"round_robin",
  "correlation_risk": "low"|"medium"|"high",
  "adjusted_ev_pct": <float>,
  "reasoning": "<2-3 sentences>",
  "risk_flags": []
}"""
    prompt = f"Evaluate this parlay:\n\n```json\n{json.dumps(legs, indent=2, default=str)}\n```"
    return _call_json(prompt, system)


# ── Discord command handlers ───────────────────────────────────────────────────

def handle_command(command: str, args: str, context: dict | None = None) -> str:
    """
    Handle Discord slash commands.
    command: analyze|player|team|parlay|explain|why|odds|results|bankroll|top-picks
    """
    prompts = {
        "analyze":   f"Analyse this betting situation: {args}",
        "player":    f"Provide a betting analysis for this player: {args}. Cover recent form, props value, injury status.",
        "team":      f"Analyse this team for betting purposes: {args}. Cover recent form, trends, injuries, matchups.",
        "parlay":    f"Evaluate this parlay for betting: {args}. Cover correlation, combined EV, risk.",
        "explain":   f"Explain this betting concept in simple terms: {args}",
        "why":       f"Explain why this bet was recommended or why it won/lost: {args}",
        "odds":      f"Explain what these odds mean and how to calculate EV: {args}",
        "results":   f"Summarise these betting results: {args}",
        "bankroll":  f"Give bankroll management advice for this situation: {args}",
        "top-picks": f"Summarise the top picks available right now based on: {args}",
    }
    prompt = prompts.get(command, f"Answer this sports betting question: {args}")
    if context:
        prompt += f"\n\nContext: {json.dumps(context, default=str)}"
    return _call_text(prompt, _DISCUSSION_SYSTEM) or "Analysis unavailable at this time."


def write_daily_summary(
    picks: list[dict],
    results: list[dict],
    clv_stats: dict,
    bankroll: float,
) -> str:
    payload = {
        "picks":    picks[:20],
        "results":  results[:20],
        "clv":      clv_stats,
        "bankroll": bankroll,
    }
    prompt = f"Write a daily summary:\n\n```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    return _call_text(prompt, _SUMMARY_SYSTEM) or "Summary unavailable."


def write_weekly_summary(stats: dict) -> str:
    prompt = f"Write a weekly performance summary:\n\n```json\n{json.dumps(stats, indent=2, default=str)}\n```"
    return _call_text(prompt, _SUMMARY_SYSTEM) or "Summary unavailable."


# ── Internal helpers ───────────────────────────────────────────────────────────

def _chat(messages: list, client: OpenAI | None = None) -> str | None:
    """Single chat call — tries direct, falls back to proxy on connection error."""
    c = client or _client
    try:
        resp = c.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=OPENAI_MAX_TOKENS,
            messages=messages,
        )
        return resp.choices[0].message.content.strip() if resp.choices else None
    except APIConnectionError as e:
        if c is _client:
            logger.warning("OpenAI direct connection failed (%s) — retrying via proxy", e)
            return _chat(messages, client=_get_proxy_client())
        logger.error("OpenAI proxy connection also failed: %s", e)
        return None
    except APIError as e:
        logger.error("OpenAI API error: %s", e)
        return None


def _call_json(prompt: str, system: str) -> dict | None:
    raw = ""
    try:
        raw = _chat([
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]) or ""
        if not raw:
            logger.warning("OpenAI returned empty content")
            return None
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0].strip()
        # Strip control characters that break JSON parsing (tabs/newlines inside strings)
        import re
        raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', ' ', raw)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("OpenAI JSON parse error: %s | raw=%r", e, raw[:200])
        return None


def _call_text(prompt: str, system: str) -> str | None:
    return _chat([
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ])
