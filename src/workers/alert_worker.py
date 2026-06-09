"""Alert worker — routes all Discord notifications asynchronously."""
import asyncio
import logging
from src.workers.celery_app import app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run a coroutine from a sync Celery context.

    asyncio.run() always creates a fresh event loop and tears it down
    cleanly — safe to call from any Celery worker thread.
    """
    return asyncio.run(coro)


@app.task
def send_pick_alerts(picks: list[dict]):
    from src.discord_bot.bot import post_pick
    for pick in picks:
        try:
            _run_async(post_pick(pick))
        except Exception as e:
            logger.error("Failed to send pick alert: %s", e)


@app.task
def send_parlay_alerts(parlays: list[dict]):
    from src.discord_bot.bot import post_parlay
    for parlay in parlays:
        try:
            _run_async(post_parlay(parlay))
        except Exception as e:
            logger.error("Failed to send parlay alert: %s", e)


@app.task
def send_line_movement_alerts(movements: list[dict]):
    from src.discord_bot.bot import post_line_movement
    for mov in movements:
        try:
            _run_async(post_line_movement(mov))
        except Exception as e:
            logger.error("Failed to send line movement alert: %s", e)


@app.task
def send_lineup_alerts(alerts: list[dict]):
    """Fire Discord alerts for injury/lineup changes that affect active props."""
    from src.discord_bot.bot import _post
    try:
        embeds = [
            {
                "title":       a["title"][:256],
                "description": a["description"][:4096],
                "color":       a["color"],
                "fields": [
                    {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
                    for f in a.get("fields", [])[:25]
                ],
            }
            for a in alerts[:10]
        ]
        import asyncio
        asyncio.run(_post({"embeds": embeds}))
        logger.info("Lineup alerts sent: %d", len(alerts))
    except Exception as e:
        logger.error("Failed to send lineup alerts: %s", e)


@app.task
def send_pp_parlay_alert(picks: list[dict]):
    from src.discord_bot.bot import post_pp_parlay
    try:
        _run_async(post_pp_parlay(picks))
    except Exception as e:
        logger.error("Failed to send PP parlay alert: %s", e)


@app.task
def send_hardrock_parlay_alert(picks: list[dict]):
    from src.discord_bot.bot import post_hardrock_parlay
    try:
        _run_async(post_hardrock_parlay(picks))
    except Exception as e:
        logger.error("Failed to send HardRock parlay alert: %s", e)


@app.task
def send_prop_pick_alerts(picks: list[dict]):
    # Disabled — individual pick cards replaced by send_prop_summary (one embed)
    logger.debug("send_prop_pick_alerts suppressed (%d picks)", len(picks))


@app.task
def send_picks_entry_post(
    prop_picks:    list[dict],
    hr_games:      list[dict],
    kalshi:        list[dict],
    polymarket:    list[dict],
    discrepancies: list[dict],
):
    """
    ONE big Picks Entry Post — Top Picks + Best Slip + HardRock + Predictions + Line Shop.
    Posted at 9:30 AM if enough quality picks exist, otherwise delayed to 10:00 AM.
    (Separate from 6 AM Good Morning Summary.)
    """
    from src.discord_bot.bot import _post
    from src.core.config import DEFAULT_BANKROLL
    import asyncio
    from datetime import datetime
    import zoneinfo

    if not prop_picks:
        return

    _SPORT_EMOJI = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "mma_mixed_martial_arts": "🥊",
        "tennis_atp_french_open": "🎾", "golf_masters_tournament_winner": "⛳",
    }
    _SPORT_LABEL = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "soccer_fifa_world_cup": "World Cup", "mma_mixed_martial_arts": "UFC/MMA",
        "tennis_atp_french_open": "Tennis", "golf_masters_tournament_winner": "Golf",
    }

    et_tz   = zoneinfo.ZoneInfo("America/New_York")
    now_str = datetime.now(et_tz).strftime("%-I:%M %p ET")
    fields  = []

    # ── Section 1: Top Picks ──────────────────────────────────────────────────
    top_lines = []
    for p in prop_picks:
        sport   = p.get("sport_key","")
        emoji   = _SPORT_EMOJI.get(sport, "🎯")
        label   = _SPORT_LABEL.get(sport, sport.split("_")[-1].upper())
        arrow   = "⬆️" if p.get("direction","").lower() == "over" else "⬇️"
        conf    = round((p.get("confidence") or 0) * 100)
        ev      = round((p.get("ev_pct") or 0) * 100, 1)
        source  = (p.get("source") or "").title()
        gt      = p.get("game_time","")
        time_str = ""
        if gt:
            try:
                from dateutil.parser import parse as _p
                t = _p(gt).astimezone(et_tz)
                time_str = f" · {t.strftime('%-I:%M %p ET')}"
            except Exception:
                pass
        top_lines.append(
            f"{emoji} **{p.get('subject')}** — {p.get('stat')} {p.get('line')} {arrow}"
            f"  `{conf}% | +{ev}% edge | {label} · {source}{time_str}`"
        )
    fields.append({"name": f"🎯 Top Picks — {now_str}", "value": "\n".join(top_lines), "inline": False})

    # ── Section 2: Best Slip (PP vs UD) ───────────────────────────────────────
    slip = _compare_slips(prop_picks)
    if slip["picks"]:
        platform = slip["platform"]
        ev_s     = slip["ev"]
        p_win    = slip["p_all_win"]
        mult     = slip["multiplier"]
        vs_plat  = slip["vs_platform"]
        vs_ev    = slip["vs_ev"]
        n        = len(slip["picks"])
        conf_avg = sum(p.get("confidence") or 0 for p in slip["picks"]) / n
        ev_avg   = sum(p.get("ev_pct") or 0 for p in slip["picks"]) / n
        bet_str  = _recommended_bet(conf_avg, ev_avg, DEFAULT_BANKROLL)
        slip_emoji = "🏆" if platform == "PrizePicks" else "🐶"
        leg_lines  = []
        for i, p in enumerate(slip["picks"], 1):
            arrow = "⬆️" if p.get("direction","").lower() == "over" else "⬇️"
            emoji = _SPORT_EMOJI.get(p.get("sport_key",""), "🎯")
            conf  = round((p.get("confidence") or 0) * 100)
            leg_lines.append(f"`{i}.` {emoji} **{p.get('subject')}** — {p.get('stat')} {p.get('line')} {arrow}  `{conf}%`")
        ev_str = f"+{round(ev_s*100,1)}%" if ev_s > 0 else f"{round(ev_s*100,1)}%"
        vs_str = f"+{round(vs_ev*100,1)}%" if vs_ev > 0 else f"{round(vs_ev*100,1)}%"
        slip_body = (
            f"{slip_emoji} **{platform}** wins — {n}-pick slip · **{mult}x**\n"
            f"`P(all win): {round(p_win*100,1)}%  ·  Slip EV: {ev_str}`  ·  {bet_str}\n"
            f"*(vs {vs_plat}: {vs_str})*\n\n"
            + "\n".join(leg_lines)
        )
        fields.append({"name": f"📋 Best Slip — {platform}  ·  {mult}x Payout", "value": slip_body, "inline": False})

    # ── Section 3: HardRock ───────────────────────────────────────────────────
    if hr_games:
        hr_lines = []
        for g in hr_games[:4]:
            sport    = _SPORT_LABEL.get(g.get("sport_key",""), g.get("sport_key","").split("_")[-1].upper())
            odds     = g.get("best_odds","")
            odds_str = (f"+{odds}" if isinstance(odds, int) and odds > 0 else str(odds)) if odds else ""
            mkt      = {"h2h":"ML","spreads":"Spread","totals":"Total"}.get(g.get("market","h2h"), "ML")
            sel      = g.get("selection","") or f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
            gt       = g.get("commence_time","")
            time_str = ""
            if gt:
                try:
                    from dateutil.parser import parse as _p
                    import zoneinfo as _zi
                    t = _p(gt).astimezone(_zi.ZoneInfo("America/New_York"))
                    time_str = f" · {t.strftime('%-I:%M %p ET')}"
                except Exception:
                    pass
            conf    = round((g.get("confidence") or 0) * 100)
            ev      = round((g.get("ev_pct") or 0) * 100, 1)
            bet_str = _recommended_bet(g.get("confidence") or 0, g.get("ev_pct") or 0, DEFAULT_BANKROLL)
            meta    = f"\n  `{conf}% · +{ev}% edge`  ·  {bet_str}" if conf else ""
            hr_lines.append(f"🪨 **{sel}**  ·  {sport}{time_str}  ·  {mkt}: {odds_str}{meta}")
        fields.append({"name": "🪨 HardRock Bet  ·  ML / Spread / Total", "value": "\n\n".join(hr_lines), "inline": False})

    # ── Section 4: Best Predictions (Kalshi vs Polymarket) ────────────────────
    best_preds = _todays_game_predictions(kalshi or [], polymarket or [])
    if best_preds:
        pred_lines = []
        for m in best_preds[:4]:
            platform  = m.get("platform","Kalshi")
            p_emoji   = "📈" if platform == "Kalshi" else "🟣"
            title     = (m.get("title") or m.get("question") or "")[:55]
            direction = (m.get("ai_direction") or "yes").upper()
            yes_pct   = round(float(m.get("yes_price",0))*100) if m.get("yes_price") else "?"
            no_pct    = round(float(m.get("no_price",0))*100)  if m.get("no_price")  else "?"
            conf      = round((m.get("ai_confidence") or 0) * 100)
            ev        = round((m.get("ai_ev_pct") or 0) * 100, 1)
            bet_str   = _recommended_bet(m.get("ai_confidence") or 0, m.get("ai_ev_pct") or 0, DEFAULT_BANKROLL)
            vs_plat   = m.get("vs_platform","")
            vs_yes    = round(float(m.get("vs_yes_price",0))*100) if m.get("vs_yes_price") else None
            vs_note   = f" *(vs {vs_plat} @ {vs_yes}¢)*" if vs_plat and vs_yes else ""
            pred_lines.append(
                f"{p_emoji} **{direction}** on **{platform}** — {title}{vs_note}\n"
                f"  YES {yes_pct}¢ / NO {no_pct}¢"
                + (f"\n  `{conf}% · +{ev}% edge`  ·  {bet_str}" if conf else "")
            )
        fields.append({"name": "🔮 Best Predictions — Today's Games  ·  Kalshi vs Polymarket", "value": "\n\n".join(pred_lines), "inline": False})

    # ── Section 5: Line Shop (if any) ─────────────────────────────────────────
    if discrepancies:
        _emoji = {
            "basketball_nba":"🏀","baseball_mlb":"⚾","americanfootball_nfl":"🏈",
            "icehockey_nhl":"🏒","soccer_epl":"⚽","mma_mixed_martial_arts":"🥊",
        }
        shop_lines = []
        for o in discrepancies[:4]:
            emoji    = _emoji.get(o.get("sport_key",""), "🎯")
            subject  = o.get("subject","?")
            stat     = o.get("stat","")
            pp_line  = o.get("pp_line")
            ud_line  = o.get("ud_line")
            gap      = o.get("gap", 0)
            best_bk  = (o.get("best_book") or "").title()
            direction = (o.get("direction") or "over").upper()
            pp_str   = f"~~{pp_line}~~" if o.get("best_book") == "underdog" else f"**{pp_line}**"
            ud_str   = f"~~{ud_line}~~" if o.get("best_book") == "prizepicks" else f"**{ud_line}**"
            shop_lines.append(
                f"{emoji} **{subject}** — {stat}  ·  PP: {pp_str}  UD: {ud_str}  Gap: **+{gap:.1f}**\n"
                f"  ✅ Bet **{direction}** on **{best_bk}**"
            )
        fields.append({"name": "💰 Line Shop — Better Odds Found", "value": "\n\n".join(shop_lines), "inline": False})

    fields.append({"name": "⚠️", "value": "Place manually on each platform. Bet responsibly.", "inline": False})

    embed = {
        "title": f"📋 Picks Entry — {datetime.now(et_tz).strftime('%A, %B %-d')}  ·  {now_str}",
        "color": 0x1A237E,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", False)}
            for f in fields
        ],
        "footer": {"text": "PrizePicks · Underdog · HardRock · Kalshi · Polymarket · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Picks entry post posted: %d picks", len(prop_picks))
    except Exception as e:
        logger.error("Failed to send picks entry post: %s", e)


@app.task
def send_prop_summary(picks: list[dict]):
    """Post ONE summary embed with all top picks — no per-pick spam."""
    from src.discord_bot.bot import _post
    from datetime import datetime
    if not picks:
        return

    # Sport display maps
    _emoji = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "basketball_ncaab": "🏀", "americanfootball_ncaaf": "🏈",
        "soccer_fifa_world_cup": "⚽", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_uefa_champs_league": "⚽", "mma_mixed_martial_arts": "🥊", "mma": "🥊",
        "tennis_atp_french_open": "🎾", "tennis": "🎾",
        "golf_masters_tournament_winner": "⛳",
    }
    _sport_name = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "basketball_ncaab": "NCAAB", "americanfootball_ncaaf": "NCAAF",
        "soccer_fifa_world_cup": "World Cup", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "soccer_uefa_champs_league": "UCL", "mma_mixed_martial_arts": "UFC/MMA", "mma": "UFC/MMA",
        "tennis_atp_french_open": "French Open", "tennis": "Tennis",
        "golf_masters_tournament_winner": "Golf",
    }

    lines = []
    for p in picks:
        sport = p.get("sport_key", "")
        emoji = _emoji.get(sport, "🎯")
        sport_label = _sport_name.get(sport, sport.replace("_", " ").title())
        direction = p.get("direction", "").upper()
        arrow = "⬆️" if direction == "OVER" else "⬇️"
        conf = round(p.get("confidence", 0) * 100)
        ev = round(p.get("ev_pct", 0) * 100, 1)
        source = p.get("source", "").title()
        game_time = p.get("game_time", "")
        time_str = ""
        if game_time:
            try:
                from dateutil.parser import parse as _parse
                import zoneinfo as _zi
                t = _parse(game_time).astimezone(_zi.ZoneInfo("America/New_York"))
                time_str = f" · {t.strftime('%-I:%M %p ET')}"
            except Exception:
                pass
        pick_type = "🏟️ Team" if p.get("is_team_prop") else "👤 Player"
        lines.append(
            f"{emoji} **{p.get('subject')}** — {p.get('stat')} {p.get('line')} {arrow} {direction}\n"
            f"  `{conf}% conf | +{ev}% edge | {sport_label} · {source} · {pick_type}{time_str}`"
        )

    import zoneinfo
    now_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
    embed = {
        "title": f"🎯 Top Prop Picks — {now_str}",
        "description": "\n\n".join(lines),
        "color": 0x00C851,
        "footer": {"text": f"{len(picks)} picks · PrizePicks & Underdog · Bet responsibly"},
    }
    try:
        import asyncio
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Prop summary posted: %d picks", len(picks))
    except Exception as e:
        logger.error("Failed to send prop summary: %s", e)


@app.task
def send_prop_result_alert(pick: dict, result: str, actual: float):
    from src.discord_bot.bot import post_prop_result
    try:
        _run_async(post_prop_result(pick, result, actual))
    except Exception as e:
        logger.error("Failed to send prop result alert: %s", e)


@app.task
def send_underdog_entry(picks: list[dict]):
    """Post Underdog Fantasy entry card — Underdog-specific props only."""
    from src.discord_bot.bot import _post
    import asyncio
    if not picks:
        return

    _SPORT_LABELS = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "basketball_ncaab": "NCAAB", "americanfootball_ncaaf": "NCAAF",
        "soccer_fifa_world_cup": "World Cup", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "mma_mixed_martial_arts": "UFC/MMA", "mma": "UFC/MMA",
        "tennis_atp_french_open": "Tennis", "tennis": "Tennis",
    }

    n = min(len(picks), 6)
    picks = picks[:n]
    # Underdog uses flex payout tiers similar to PP
    _UD_MULTIPLIERS = {2: 3, 3: 6, 4: 10, 5: 20, 6: 40}
    mult = _UD_MULTIPLIERS.get(n, 40)
    avg_conf = round(sum((p.get("confidence") or 0) for p in picks) / n * 100)

    leg_blocks = []
    for i, p in enumerate(picks, 1):
        arrow = "↑" if (p.get("direction") or "over").lower() == "over" else "↓"
        sport = _SPORT_LABELS.get(p.get("sport_key", ""), p.get("sport_key", "").split("_")[-1].upper())
        subject = p.get("subject", "?")
        stat = p.get("stat", "")
        line = p.get("line", "?")
        conf = round((p.get("confidence") or 0) * 100)
        ev = round((p.get("ev_pct") or 0) * 100, 1)
        factors = p.get("key_factors") or []
        reasoning = (p.get("reasoning") or "").strip()
        top_reason = factors[0] if factors else (reasoning.split(".")[0].strip() if reasoning else "—")
        leg_blocks.append(
            f"`{i}.` {arrow} **{subject}** — {line} {stat}  ·  {sport}  ·  {conf}% conf  ·  +{ev}% edge\n"
            f"     └ {top_reason}"
        )

    fields = [
        {"name": "Legs",       "value": str(n),         "inline": True},
        {"name": "Multiplier", "value": f"**{mult}x**", "inline": True},
        {"name": "Avg Conf",   "value": f"{avg_conf}%", "inline": True},
        {"name": "⚠️", "value": "Place manually on Underdog Fantasy. Max 6 picks.", "inline": False},
    ]
    embed = {
        "title": f"🐶 Underdog Entry — {n} Picks  ·  {mult}x Payout",
        "description": "\n".join(leg_blocks),
        "color": 0xE65100,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
            for f in fields
        ],
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Underdog entry posted: %d picks", n)
    except Exception as e:
        logger.error("Failed to send Underdog entry: %s", e)


def _recommended_bet(confidence: float, ev_pct: float,
                     bankroll: float = 1000.0,   # kept for signature compat, unused
                     min_bet: int = 5, max_bet: int = 100) -> str:
    """
    Tiered bet sizing in the $5–$100 range.
    Confidence is the primary driver; EV adds a small boost (up to +25%).
    All amounts rounded to the nearest $5.

    Tier table (before EV boost):
      60–64%  →  $5
      65–69%  →  $10
      70–74%  →  $20
      75–79%  →  $35
      80–84%  →  $50
      85–89%  →  $75
      90%+    →  $85
    """
    if not confidence or confidence < 0.60 or not ev_pct or ev_pct <= 0:
        return ""

    if confidence >= 0.90:
        base = 85
    elif confidence >= 0.85:
        base = 75
    elif confidence >= 0.80:
        base = 50
    elif confidence >= 0.75:
        base = 35
    elif confidence >= 0.70:
        base = 20
    elif confidence >= 0.65:
        base = 10
    else:
        base = 5

    # EV boost: up to +25% on top of base (capped)
    ev_boost = min(1.25, 1.0 + ev_pct * 2)
    raw = base * ev_boost
    dollar = int(round(raw / 5) * 5)          # round to nearest $5
    dollar = max(min_bet, min(max_bet, dollar))

    # Confidence label so user knows why
    if confidence >= 0.85:
        label = "🔥 High confidence"
    elif confidence >= 0.75:
        label = "✅ Good confidence"
    elif confidence >= 0.65:
        label = "👍 Moderate confidence"
    else:
        label = "⚠️ Low confidence"

    return f"**Put down ${dollar}** — {label} ({round(confidence*100)}% / +{round(ev_pct*100,1)}% edge)"


def _slip_ev(picks: list[dict], platform: str) -> tuple[float, float, int]:
    """
    Calculate expected value of an entire slip.
    Returns (ev, p_all_win, multiplier).

    PP Power multipliers (all must win): 2=3x, 3=5x, 4=10x, 5=20x, 6=40x
    PP Flex (3-6, 1 loss = reduced payout): discounted ~40% vs Power
    Underdog: 2=3x, 3=6x, 4=10x, 5=20x, 6=40x
    """
    _PP_POWER = {2: 3, 3: 5, 4: 10, 5: 20, 6: 40}
    _UD_MULT  = {2: 3, 3: 6, 4: 10, 5: 20, 6: 40}

    n = min(len(picks), 6)
    if n == 0:
        return 0.0, 0.0, 0

    # Probability all legs win (independence assumption)
    p_all = 1.0
    for p in picks[:n]:
        p_all *= float(p.get("confidence") or 0.55)

    if platform == "prizepicks":
        mult = _PP_POWER.get(n, 40)
    else:
        mult = _UD_MULT.get(n, 40)

    ev = p_all * mult - 1.0   # EV per $1 risked
    return round(ev, 4), round(p_all, 4), mult


def _compare_slips(prop_picks: list[dict]) -> dict:
    """
    Compare entire PP slip vs entire UD slip.
    Returns dict with: platform, picks, ev, p_all_win, multiplier, vs_ev, vs_platform.
    Shows which slip gives better expected return and why.
    """
    pp = [p for p in prop_picks if p.get("source") == "prizepicks"][:6]
    ud = [p for p in prop_picks if p.get("source") == "underdog"][:6]

    pp_ev, pp_p, pp_mult = _slip_ev(pp, "prizepicks")
    ud_ev, ud_p, ud_mult = _slip_ev(ud, "underdog")

    if pp_ev >= ud_ev:
        return {
            "platform": "PrizePicks", "picks": pp,
            "ev": pp_ev, "p_all_win": pp_p, "multiplier": pp_mult,
            "vs_platform": "Underdog", "vs_ev": ud_ev,
        }
    else:
        return {
            "platform": "Underdog", "picks": ud,
            "ev": ud_ev, "p_all_win": ud_p, "multiplier": ud_mult,
            "vs_platform": "PrizePicks", "vs_ev": pp_ev,
        }


def _todays_game_predictions(kalshi: list[dict], polymarket: list[dict]) -> list[dict]:
    """
    Filter Kalshi + Polymarket markets to only predictions about TODAY's live/upcoming games.
    Matches by team names pulled from Redis active picks + today's odds cache.
    Then for each matched prediction, picks the better-priced platform (Kalshi vs Poly).
    Returns flat list, best platform per prediction, sorted by confidence.
    """
    import json
    from datetime import datetime
    import zoneinfo
    from src.core.config import REDIS_URL
    import redis as _redis

    r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    et_tz = zoneinfo.ZoneInfo("America/New_York")
    today = datetime.now(et_tz).strftime("%Y-%m-%d")

    # Gather team/player keywords from today's active picks
    keywords: set[str] = set()
    for key in ("props:active_picks", "props:all"):
        raw = r.get(key)
        if not raw:
            continue
        try:
            items = json.loads(raw)
            for p in items:
                for field in ("team", "opponent", "subject", "home_team", "away_team"):
                    val = (p.get(field) or "").strip()
                    if val and len(val) > 2:
                        # Add full name and last word (surname / city shorthand)
                        keywords.add(val.lower())
                        keywords.add(val.split()[-1].lower())
        except Exception:
            continue

    # Also pull from odds cache if available
    for sport_key in ("basketball_nba", "baseball_mlb", "soccer_epl", "soccer_usa_mls",
                      "americanfootball_nfl", "icehockey_nhl", "mma_mixed_martial_arts"):
        raw = r.get(f"odds:{sport_key}")
        if not raw:
            continue
        try:
            for game in json.loads(raw):
                for field in ("home_team", "away_team"):
                    val = (game.get(field) or "").strip()
                    if val:
                        keywords.add(val.lower())
                        keywords.add(val.split()[-1].lower())
        except Exception:
            continue

    def _matches_today(market: dict) -> bool:
        title = (market.get("title") or market.get("question") or "").lower()
        if not keywords:
            return True   # no game data — show all
        return any(kw in title for kw in keywords if len(kw) > 3)

    # Tag and filter
    matched: list[dict] = []
    for m in kalshi:
        if _matches_today(m):
            m["platform"] = "Kalshi"
            matched.append(m)
    for m in polymarket:
        if _matches_today(m):
            m["platform"] = "Polymarket"
            matched.append(m)

    if not matched:
        # Fallback: show all AI-scored markets if no game data in Redis
        for m in kalshi:
            m["platform"] = "Kalshi"
        for m in polymarket:
            m["platform"] = "Polymarket"
        matched = list(kalshi) + list(polymarket)

    # Deduplicate: for same prediction on both platforms, keep better-priced one
    from collections import defaultdict
    norm = lambda t: (t or "").lower().strip()[:80]
    by_title: dict[str, list[dict]] = defaultdict(list)
    for m in matched:
        by_title[norm(m.get("title") or m.get("question") or "")].append(m)

    result = []
    for items in by_title.values():
        if len(items) == 1:
            result.append(items[0])
        else:
            direction = (items[0].get("ai_direction") or "yes").lower()
            if direction == "yes":
                winner = min(items, key=lambda x: float(x.get("yes_price") or 1))
            else:
                winner = min(items, key=lambda x: float(x.get("no_price") or 1))
            loser = [i for i in items if i is not winner][0]
            winner["vs_platform"] = loser.get("platform")
            winner["vs_yes_price"] = loser.get("yes_price")
            result.append(winner)

    result.sort(key=lambda m: float(m.get("ai_confidence") or 0), reverse=True)
    return result


@app.task
def send_combined_entry(
    prop_picks: list[dict],
    hr_games:   list[dict],
    kalshi:     list[dict],
    polymarket: list[dict],
):
    """
    ONE combined entry card.
    - Props: compare FULL PP slip EV vs FULL UD slip EV — show only the better slip
    - HardRock: always shown (ML / Spread / Total — no competing platform)
    - Predictions: Kalshi vs Polymarket filtered to TODAY's live/upcoming games,
      better-priced platform shown per prediction
    - Each section shows recommended bet size
    """
    from src.discord_bot.bot import _post
    from src.core.config import DEFAULT_BANKROLL
    import asyncio

    _SPORT_EMOJI = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "mma_mixed_martial_arts": "🥊",
        "tennis_atp_french_open": "🎾", "golf_masters_tournament_winner": "⛳",
    }
    _SPORT_LABEL = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "soccer_epl": "EPL", "soccer_usa_mls": "MLS",
        "soccer_fifa_world_cup": "World Cup", "mma_mixed_martial_arts": "UFC/MMA",
        "tennis_atp_french_open": "Tennis", "golf_masters_tournament_winner": "Golf",
    }

    fields = []

    # ── Best Slip: PP vs Underdog (slip EV comparison) ─────────────────────────
    if prop_picks:
        slip = _compare_slips(prop_picks)
        picks = slip["picks"]
        if picks:
            platform = slip["platform"]
            ev       = slip["ev"]
            p_win    = slip["p_all_win"]
            mult     = slip["multiplier"]
            vs_plat  = slip["vs_platform"]
            vs_ev    = slip["vs_ev"]
            n        = len(picks)

            slip_emoji = "🏆" if platform == "PrizePicks" else "🐶"
            ev_pct_avg = sum(p.get("ev_pct") or 0 for p in picks) / n
            conf_avg   = sum(p.get("confidence") or 0 for p in picks) / n
            bet_str    = _recommended_bet(conf_avg, ev_pct_avg, DEFAULT_BANKROLL)

            leg_lines = []
            for i, p in enumerate(picks, 1):
                arrow = "⬆️" if p.get("direction","").lower() == "over" else "⬇️"
                emoji = _SPORT_EMOJI.get(p.get("sport_key",""), "🎯")
                conf  = round((p.get("confidence") or 0) * 100)
                leg_lines.append(
                    f"`{i}.` {emoji} **{p.get('subject')}** — {p.get('stat')} {p.get('line')} {arrow}  `{conf}%`"
                )

            ev_str  = f"+{round(ev*100,1)}%" if ev > 0 else f"{round(ev*100,1)}%"
            vs_str  = f"+{round(vs_ev*100,1)}%" if vs_ev > 0 else f"{round(vs_ev*100,1)}%"
            summary = (
                f"{slip_emoji} **{platform}** wins — {n}-pick slip · **{mult}x** payout\n"
                f"`P(all win): {round(p_win*100,1)}% · Slip EV: {ev_str}` · {bet_str}\n"
                f"*(vs {vs_plat} slip EV: {vs_str})*\n\n"
                + "\n".join(leg_lines)
            )
            fields.append({"name": f"🎯 Best Slip — {platform}  ·  {mult}x", "value": summary, "inline": False})

    # ── HardRock (always shown — ML / Spread / Total) ──────────────────────────
    if hr_games:
        lines = []
        for g in hr_games[:4]:
            sport    = _SPORT_LABEL.get(g.get("sport_key",""), g.get("sport_key","").split("_")[-1].upper())
            odds     = g.get("best_odds", "")
            odds_str = (f"+{odds}" if isinstance(odds, int) and odds > 0 else str(odds)) if odds else ""
            mkt      = {"h2h": "ML", "spreads": "Spread", "totals": "Total"}.get(g.get("market","h2h"), g.get("market","h2h").title())
            sel      = g.get("selection","") or f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
            game_time = g.get("commence_time","")
            time_str = ""
            if game_time:
                try:
                    from dateutil.parser import parse as _p
                    import zoneinfo as _zi
                    t = _p(game_time).astimezone(_zi.ZoneInfo("America/New_York"))
                    time_str = f" · {t.strftime('%-I:%M %p ET')}"
                except Exception:
                    pass
            conf    = round((g.get("confidence") or 0) * 100)
            ev      = round((g.get("ev_pct") or 0) * 100, 1)
            bet_str = _recommended_bet(g.get("confidence") or 0, g.get("ev_pct") or 0, DEFAULT_BANKROLL)
            meta    = f"\n  `{conf}% conf · +{ev}% edge` · {bet_str}" if conf else ""
            lines.append(
                f"🪨 **{sel}**  ·  {sport}{time_str}  ·  {mkt}: **{sel}** {odds_str}" + meta
            )
        fields.append({"name": "🪨 HardRock Bet  ·  ML / Spread / Total", "value": "\n\n".join(lines), "inline": False})

    # ── Best Predictions: today's games on Kalshi vs Polymarket ────────────────
    best_preds = _todays_game_predictions(kalshi or [], polymarket or [])
    if best_preds:
        lines = []
        for m in best_preds[:5]:
            platform  = m.get("platform", "Kalshi")
            p_emoji   = "📈" if platform == "Kalshi" else "🟣"
            title     = (m.get("title") or m.get("question") or "")[:55]
            direction = (m.get("ai_direction") or "yes").upper()
            yes_pct   = round(float(m.get("yes_price",0))*100) if m.get("yes_price") else "?"
            no_pct    = round(float(m.get("no_price",0))*100)  if m.get("no_price")  else "?"
            conf      = round((m.get("ai_confidence") or 0) * 100)
            ev        = round((m.get("ai_ev_pct") or 0) * 100, 1)
            bet_str   = _recommended_bet(m.get("ai_confidence") or 0, m.get("ai_ev_pct") or 0, DEFAULT_BANKROLL)
            vs_plat   = m.get("vs_platform","")
            vs_yes    = round(float(m.get("vs_yes_price",0))*100) if m.get("vs_yes_price") else None
            vs_note   = f" *(vs {vs_plat} @ {vs_yes}¢)*" if vs_plat and vs_yes else ""
            price_str = f"YES {yes_pct}¢ / NO {no_pct}¢"
            meta      = f"`{conf}% conf · +{ev}% edge` · {bet_str}" if conf else ""
            lines.append(
                f"{p_emoji} **{direction}** on **{platform}** — {title}{vs_note}\n"
                f"  {price_str}" + (f"\n  {meta}" if meta else "")
            )
        fields.append({"name": "🔮 Best Predictions — Today's Games  ·  Kalshi vs Polymarket", "value": "\n\n".join(lines), "inline": False})

    if not fields:
        return

    fields.append({"name": "⚠️", "value": "Place manually on each platform. Bet responsibly.", "inline": False})

    from datetime import datetime
    import zoneinfo
    now_str = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
    embed = {
        "title": f"📋 Today's Entries — {now_str}",
        "color": 0x1A237E,
        "fields": [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", False)}
            for f in fields
        ],
        "footer": {"text": "Best slip · HardRock · Best prediction odds on today's games · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Combined entry posted")
    except Exception as e:
        logger.error("Failed to send combined entry: %s", e)


@app.task
def send_hardrock_entry(games: list[dict]):
    """Post HardRock Bet entry card — top game picks (ML/spread/totals) from Odds API."""
    from src.discord_bot.bot import _post
    import asyncio
    if not games:
        return

    _SPORT_LABELS = {
        "basketball_nba": "NBA", "baseball_mlb": "MLB", "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL", "basketball_ncaab": "NCAAB", "americanfootball_ncaaf": "NCAAF",
        "soccer_epl": "EPL", "soccer_usa_mls": "MLS", "soccer_fifa_world_cup": "World Cup",
        "mma_mixed_martial_arts": "UFC/MMA", "tennis_atp_french_open": "Tennis",
    }
    _MARKET_LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}

    n = min(len(games), 4)
    games = games[:n]

    leg_lines = []
    for i, g in enumerate(games, 1):
        home  = g.get("home_team", "?")
        away  = g.get("away_team", "?")
        sport = _SPORT_LABELS.get(g.get("sport_key", ""), g.get("sport_key", "").split("_")[-1].upper())
        odds  = g.get("best_odds", "")
        book  = g.get("book", "")
        mkt   = _MARKET_LABELS.get(g.get("market", "h2h"), g.get("market", "h2h").title())
        sel   = g.get("selection", "")
        odds_str = f"+{odds}" if isinstance(odds, int) and odds > 0 else str(odds)

        game_time = g.get("commence_time", "")
        time_str = ""
        if game_time:
            try:
                from dateutil.parser import parse as _parse
                import zoneinfo as _zi
                t = _parse(game_time).astimezone(_zi.ZoneInfo("America/New_York"))
                time_str = f" · {t.strftime('%-I:%M %p ET')}"
            except Exception:
                pass

        leg_lines.append(
            f"`{i}.` **{away} @ {home}**  ·  {sport}{time_str}\n"
            f"     └ {mkt}: **{sel}**  ·  {odds_str}  ·  via {book}"
        )

    embed = {
        "title": f"🪨 HardRock Entry — {n} Games",
        "description": "\n".join(leg_lines),
        "color": 0xB71C1C,
        "fields": [
            {"name": "⚠️", "value": "Place manually on HardRock Bet. Parlay for bigger payout.", "inline": False},
        ],
        "footer": {"text": "HardRock Bet · via Odds API · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("HardRock entry posted: %d games", n)
    except Exception as e:
        logger.error("Failed to send HardRock entry: %s", e)


@app.task
def send_kalshi_entry(markets: list[dict]):
    """Post Kalshi AI-scored prediction market entry card."""
    from src.discord_bot.bot import _post
    import asyncio
    if not markets:
        return

    n = min(len(markets), 6)
    markets = markets[:n]

    lines = []
    for i, m in enumerate(markets, 1):
        title_text  = m.get("title") or m.get("question") or m.get("market_id", "?")
        yes_price   = m.get("yes_price")
        no_price    = m.get("no_price")
        direction   = m.get("ai_direction", "yes").upper()
        confidence  = m.get("ai_confidence")
        reasoning   = m.get("ai_reasoning", "")
        ev_pct      = m.get("ai_ev_pct", 0)

        yes_pct = round(float(yes_price) * 100) if yes_price is not None else "?"
        no_pct  = round(float(no_price)  * 100) if no_price  is not None else "?"
        price_str = f"YES {yes_pct}¢  /  NO {no_pct}¢"

        conf_str = f"{round(confidence * 100)}% conf" if confidence else ""
        ev_str   = f"+{round(ev_pct * 100, 1)}% edge" if ev_pct else ""
        meta     = "  ·  ".join(filter(None, [conf_str, ev_str]))

        lines.append(
            f"`{i}.` ✅ Bet **{direction}** — **{title_text}**\n"
            f"     └ {price_str}" + (f"  ·  {meta}" if meta else "")
            + (f"\n     └ {reasoning}" if reasoning else "")
        )

    embed = {
        "title": f"📈 Kalshi Entry — {n} Predictions",
        "description": "\n\n".join(lines),
        "color": 0x00ACC1,
        "fields": [
            {"name": "⚠️", "value": "Place manually on Kalshi. Prediction markets — not a sportsbook.", "inline": False},
        ],
        "footer": {"text": "Kalshi · AI-scored sports predictions · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Kalshi entry posted: %d markets", n)
    except Exception as e:
        logger.error("Failed to send Kalshi entry: %s", e)


@app.task
def send_games_starting_soon(games: list[dict]):
    """Post ONE embed: games with active picks starting in ~30 min."""
    from src.discord_bot.bot import _post
    import asyncio
    if not games:
        return

    _SPORT_EMOJI = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "mma_mixed_martial_arts": "🥊",
        "tennis_atp_french_open": "🎾", "golf_masters_tournament_winner": "⛳",
    }

    lines = []
    for g in games:
        sport    = g.get("sport_key", "")
        emoji    = _SPORT_EMOJI.get(sport, "🎯")
        team     = g.get("team", g.get("home_team", "?"))
        opponent = g.get("opponent", g.get("away_team", "?"))
        gt       = g.get("game_time_et", "")
        subject  = g.get("subject", "")
        stat     = g.get("stat", "")
        direction = (g.get("direction") or "").upper()
        line     = g.get("line", "")
        arrow    = "⬆️" if direction == "OVER" else "⬇️" if direction == "UNDER" else ""
        pick_str = f" — {subject} {stat} {line} {arrow}" if subject else ""
        lines.append(f"{emoji} **{team} vs {opponent}**  ·  {gt}{pick_str}")

    n = len(games)
    title = "⚠️ Game Starting Soon — Active Pick Inside" if n == 1 else "⚠️ Games Starting Soon — Active Picks Inside"
    embed = {
        "title": title,
        "description": "\n".join(lines),
        "color": 0xF57F17,
        "footer": {"text": "~30 min to tip — last chance to place your bets"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Games-starting-soon alert sent: %d games", len(games))
    except Exception as e:
        logger.error("Failed to send games-starting-soon alert: %s", e)


@app.task
def send_games_started(games: list[dict]):
    """Post ONE embed: games with active picks that just went live."""
    from src.discord_bot.bot import _post
    import asyncio
    if not games:
        return

    _SPORT_EMOJI = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "mma_mixed_martial_arts": "🥊",
        "tennis_atp_french_open": "🎾", "golf_masters_tournament_winner": "⛳",
    }

    lines = []
    for g in games:
        sport    = g.get("sport_key", "")
        emoji    = _SPORT_EMOJI.get(sport, "🎯")
        team     = g.get("team", g.get("home_team", "?"))
        opponent = g.get("opponent", g.get("away_team", "?"))
        subject  = g.get("subject", "")
        stat     = g.get("stat", "")
        direction = (g.get("direction") or "").upper()
        line     = g.get("line", "")
        arrow    = "⬆️" if direction == "OVER" else "⬇️" if direction == "UNDER" else ""
        pick_str = f" — {subject} {stat} {line} {arrow}" if subject else ""
        lines.append(f"{emoji} **{team} vs {opponent}**  ·  LIVE{pick_str}")

    n = len(games)
    title = "🔴 Game is Live" if n == 1 else "🔴 Games are Live"
    embed = {
        "title": title,
        "description": "\n".join(lines),
        "color": 0xFF1744,
        "footer": {"text": "Games underway — track your picks"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Games-started alert sent: %d games", len(games))
    except Exception as e:
        logger.error("Failed to send games-started alert: %s", e)


@app.task
def send_games_result(results: list[dict]):
    """
    Post ONE embed per settled pick with progress counter.
    Shows: "Messi Shot on Goal 2.5 ✓ 1/3 picks left to go"
    When ALL picks settle, appends the final slip verdict (WIN / LOSS / REDUCED).

    Each result dict should have:
      subject, stat, line, direction, actual_value, outcome (won/lost/push),
      sport_key, team, opponent, slip_type ("pp_power"|"pp_flex"|"underdog"|"hardrock"),
      session_key (optional — defaults to today's date string)
    """
    from src.discord_bot.bot import _post
    import asyncio
    import json
    from datetime import datetime
    import zoneinfo
    from src.core.config import REDIS_URL
    import redis as _redis

    if not results:
        return

    r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
    et_tz   = zoneinfo.ZoneInfo("America/New_York")
    today   = datetime.now(et_tz).strftime("%Y-%m-%d")

    _SPORT_EMOJI = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "mma_mixed_martial_arts": "🥊",
        "tennis_atp_french_open": "🎾", "golf_masters_tournament_winner": "⛳",
    }

    lines = []
    for res in results:
        sport     = res.get("sport_key", "")
        emoji     = _SPORT_EMOJI.get(sport, "🎯")
        subject   = res.get("subject", "?")
        stat      = res.get("stat", "")
        line      = res.get("line", "")
        direction = (res.get("direction") or "").upper()
        actual    = res.get("actual_value", "?")
        outcome   = res.get("outcome", "").lower()
        slip_type = res.get("slip_type", "pp_flex")
        session   = res.get("session_key", today)

        outcome_emoji = "✅" if outcome == "won" else "❌" if outcome == "lost" else "➖"

        # ── Progress tracking via Redis ──────────────────────────────────────
        progress_key = f"slip:progress:{session}"
        total_key    = f"slip:total:{session}"
        losses_key   = f"slip:losses:{session}"

        # Seed total on first result of this session
        total_raw = r.get(total_key)
        if not total_raw:
            # Try to get total from active_picks cache
            active_raw = r.get("props:active_picks")
            total = len(json.loads(active_raw)) if active_raw else 1
            r.setex(total_key, 86400, total)
        else:
            total = int(total_raw)

        settled = r.incr(progress_key)
        r.expire(progress_key, 86400)
        if outcome == "lost":
            r.incr(losses_key)
            r.expire(losses_key, 86400)
        losses = int(r.get(losses_key) or 0)

        left = total - settled
        if left > 0:
            progress_str = f"  `{left}/{total} picks left to go`"
        else:
            progress_str = "  `All picks settled`"

        lines.append(
            f"{emoji} {outcome_emoji} **{subject}** — {stat} {line} {direction}\n"
            f"  actual: **{actual}** → **{outcome.upper()}**\n{progress_str}"
        )

        # ── Final slip verdict when last pick settles ────────────────────────
        if settled >= total:
            verdict = _slip_verdict(slip_type, total, losses)
            lines.append(f"\n{verdict}")

    if not lines:
        return

    n = len(results)
    title = "📊 Pick Result" if n == 1 else "📊 Pick Results"
    embed = {
        "title": title,
        "description": "\n\n".join(lines),
        "color": 0x4CAF50,
        "footer": {"text": "Results for your active picks today"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Game results alert sent: %d results", n)
    except Exception as e:
        logger.error("Failed to send game results alert: %s", e)


def _slip_verdict(slip_type: str, total: int, losses: int) -> str:
    """
    Return the final slip verdict string based on slip type and loss count.

    PP Power Play 2: both must win — 1 loss = dead
    PP Power Play 3-6: 1 loss = entire slip dead
    PP Flex 3-6: 1 loss = reduced payout (slip still alive), 2+ = dead
    Underdog: 1 loss = dead
    HardRock parlay: 1 loss = dead
    """
    if losses == 0:
        return "🏆 **SLIP WIN** — All picks cashed! Full payout."

    if slip_type == "pp_flex" and total >= 3:
        # Flex: 1 loss = reduced, 2+ = dead
        if losses == 1:
            return "⚠️ **SLIP REDUCED** — 1 loss on Flex slip. Reduced payout — still counts."
        else:
            return "💀 **SLIP LOST** — 2+ losses on PP Flex slip. No payout."

    if slip_type == "pp_power" and total == 2:
        return "💀 **SLIP LOST** — PP Power Play (2-pick): 1 loss kills the slip."

    if slip_type == "pp_power" and total >= 3:
        return "💀 **SLIP LOST** — PP Power Play: any loss kills the slip."

    if slip_type == "underdog":
        return "💀 **SLIP LOST** — Underdog: 1 loss = dead slip. No payout."

    if slip_type == "hardrock":
        return "💀 **SLIP LOST** — HardRock parlay: 1 loss = dead slip. No payout."

    if slip_type == "kalshi":
        return "💀 **PREDICTION LOST** — Kalshi: 1 wrong call = dead entry. No payout."

    if slip_type == "polymarket":
        return "💀 **PREDICTION LOST** — Polymarket: 1 wrong call = dead entry. No payout."

    # Default fallback
    return "💀 **SLIP LOST**" if losses > 0 else "🏆 **SLIP WIN**"


@app.task
def send_prop_change_alerts(changes: list[dict]):
    # Disabled — floods Discord with thousands of line moves every scan
    logger.debug("send_prop_change_alerts suppressed (%d changes)", len(changes))


@app.task
def send_pick_line_update(changes: list[dict]):
    """
    🚨 ALERT ALERT — fires when any of our chosen picks moves line or goes off-board.
    All changes batched into ONE summary embed.
    """
    from src.discord_bot.bot import _post
    import asyncio
    if not changes:
        return

    lines = []
    for c in changes:
        subject   = c.get("subject", "Unknown")
        stat      = c.get("stat", "")
        direction = c.get("our_direction", "").upper()
        sport     = c.get("sport_key", "").split("_")[-1].upper()

        if c.get("change_type") == "moved":
            old = c.get("old_line")
            new = c.get("new_line")
            arrow = "⬆️" if (new or 0) > (old or 0) else "⬇️"
            favorable = (
                (direction == "OVER"  and (new or 0) < (old or 0)) or
                (direction == "UNDER" and (new or 0) > (old or 0))
            )
            favor_str = "✅ Favourable move" if favorable else "⚠️ Unfavourable move"
            lines.append(
                f"📊 **{subject}** — {stat} · {sport}\n"
                f"  Line: ~~{old}~~ → **{new}** {arrow}  ·  Our pick: **{direction}**  ·  {favor_str}"
            )
        elif c.get("change_type") == "removed":
            lines.append(
                f"❌ **{subject}** — {stat} · {sport}\n"
                f"  **OFF THE BOARD** — our pick ({direction}) is no longer available"
            )

    if not lines:
        return

    embed = {
        "title": "🚨 ALERT ALERT — Prop Updated",
        "description": (
            f"**{len(lines)} of your active picks have been updated.**\n\n"
            + "\n\n".join(lines)
        ),
        "color": 0xFF0000,
        "footer": {"text": "Review before placing — lines may have shifted"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Pick line ALERT sent: %d changes", len(changes))
    except Exception as e:
        logger.error("Failed to send pick line alert: %s", e)


@app.task
def send_watchlist_update(watchlist: list[dict]):
    # Disabled — Props on Radar removed from Discord per user preference
    logger.debug("send_watchlist_update suppressed (%d props)", len(watchlist))


@app.task
def send_polymarket_entry(markets: list[dict]):
    """Post Polymarket AI-scored sports prediction entry card."""
    from src.discord_bot.bot import _post
    import asyncio
    if not markets:
        return

    n = min(len(markets), 6)
    markets = markets[:n]

    lines = []
    for i, m in enumerate(markets, 1):
        title_text = m.get("title") or m.get("question") or m.get("market_id", "?")
        yes_price  = m.get("yes_price")
        no_price   = m.get("no_price")
        direction  = m.get("ai_direction", "yes").upper()
        confidence = m.get("ai_confidence")
        reasoning  = m.get("ai_reasoning", "")
        ev_pct     = m.get("ai_ev_pct", 0)
        outcomes   = m.get("outcomes", ["Yes", "No"])
        volume     = m.get("volume", 0)

        yes_pct = round(float(yes_price) * 100) if yes_price is not None else "?"
        no_pct  = round(float(no_price)  * 100) if no_price  is not None else "?"

        # For team vs team markets, show outcome names
        if outcomes and len(outcomes) == 2 and outcomes[0].lower() not in ("yes", "no"):
            price_str = f"{outcomes[0]}: {yes_pct}¢  ·  {outcomes[1]}: {no_pct}¢"
            bet_label = outcomes[0] if direction == "YES" else outcomes[1]
        else:
            price_str = f"YES {yes_pct}¢  /  NO {no_pct}¢"
            bet_label = direction

        conf_str = f"{round(confidence * 100)}% conf" if confidence else ""
        ev_str   = f"+{round(ev_pct * 100, 1)}% edge" if ev_pct else ""
        vol_str  = f"${volume:,.0f} vol" if volume else ""
        meta     = "  ·  ".join(filter(None, [conf_str, ev_str, vol_str]))

        lines.append(
            f"`{i}.` ✅ Bet **{bet_label}** — **{title_text}**\n"
            f"     └ {price_str}" + (f"  ·  {meta}" if meta else "")
            + (f"\n     └ {reasoning}" if reasoning else "")
        )

    embed = {
        "title": f"🟣 Polymarket Entry — {n} Predictions",
        "description": "\n\n".join(lines),
        "color": 0x6C3FC5,
        "fields": [
            {"name": "⚠️", "value": "Place manually on Polymarket. Decentralised prediction market — USDC on Polygon.", "inline": False},
        ],
        "footer": {"text": "Polymarket · AI-scored sports predictions · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Polymarket entry posted: %d markets", n)
    except Exception as e:
        logger.error("Failed to send Polymarket entry: %s", e)


@app.task
def send_line_shop_alert(opportunities: list[dict]):
    """
    💰 LINE SHOP — same prop, different lines on PP vs Underdog.
    One embed showing all discrepancies, ranked by gap size.
    """
    from src.discord_bot.bot import _post
    import asyncio
    if not opportunities:
        return

    _emoji = {
        "basketball_nba": "🏀", "baseball_mlb": "⚾", "americanfootball_nfl": "🏈",
        "icehockey_nhl": "🏒", "basketball_ncaab": "🏀", "americanfootball_ncaaf": "🏈",
        "mma_mixed_martial_arts": "🥊", "soccer_epl": "⚽", "soccer_usa_mls": "⚽",
        "soccer_fifa_world_cup": "⚽", "tennis_atp_french_open": "🎾",
    }

    lines = []
    for o in opportunities[:10]:
        emoji = _emoji.get(o.get("sport_key", ""), "🎯")
        subject = o.get("subject", "?")
        stat = o.get("stat", "")
        pp_line = o.get("pp_line")
        ud_line = o.get("ud_line")
        gap = o.get("gap", 0)
        best_book = o.get("best_book", "").title()
        direction = o.get("direction", "over").upper()
        edge_note = o.get("edge_note", "")

        pp_str = f"~~{pp_line}~~" if o.get("best_book") == "underdog" else f"**{pp_line}**"
        ud_str = f"~~{ud_line}~~" if o.get("best_book") == "prizepicks" else f"**{ud_line}**"

        lines.append(
            f"{emoji} **{subject}** — {stat}\n"
            f"  PP: {pp_str}  ·  UD: {ud_str}  ·  Gap: **+{gap:.1f} pts**\n"
            f"  ✅ Bet **{direction}** on **{best_book}** — {edge_note}"
        )

    embed = {
        "title": "💰 Line Shop Alert — Better Odds Available",
        "description": (
            "Same prop, different lines. Take the easier side.\n\n"
            + "\n\n".join(lines)
        ),
        "color": 0xFFD700,
        "footer": {"text": f"{len(opportunities)} discrepancies found · PrizePicks vs Underdog · Bet responsibly"},
    }
    try:
        asyncio.run(_post({"embeds": [embed]}))
        logger.info("Line shop alert posted: %d opportunities", len(opportunities))
    except Exception as e:
        logger.error("Failed to send line shop alert: %s", e)


@app.task
def send_result_alert(pick: dict, result: str):
    from src.discord_bot.bot import post_result
    try:
        _run_async(post_result(pick, result))
    except Exception as e:
        logger.error("Failed to send result alert: %s", e)


@app.task
def send_pregame_alerts():
    """
    Check active picks and fire game alerts ONLY for games we have a pick on.
    Fires for ANY sport — soccer, MLB, NBA, UFC, etc.
    Uses Redis props cache (not DB) so it works even if games table is sparse.
    """
    import json
    from datetime import datetime, timezone, timedelta
    from dateutil.parser import parse as _parse
    import zoneinfo
    from src.core.config import REDIS_URL
    import redis as _redis

    r = _redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)

    # Get active picks from cache
    active_raw = r.get("props:active_picks")
    if not active_raw:
        return
    active_picks = json.loads(active_raw)
    if not active_picks:
        return

    et_tz  = zoneinfo.ZoneInfo("America/New_York")
    now    = datetime.now(et_tz)

    # Build unique matchups from active picks
    seen_keys: set = set()
    matchups: list[dict] = []
    for p in active_picks:
        gt = p.get("game_time", "")
        if not gt:
            continue
        team     = p.get("team", "")
        opponent = p.get("opponent", "")
        key      = f"{team}|{opponent}|{gt}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        matchups.append({
            "team":       team,
            "opponent":   opponent,
            "sport_key":  p.get("sport_key", ""),
            "game_time":  gt,
            "subject":    p.get("subject", ""),
            "stat":       p.get("stat", ""),
            "direction":  p.get("direction", ""),
            "line":       p.get("line", ""),
        })

    starting_soon: list[dict] = []
    just_started:  list[dict] = []

    for m in matchups:
        try:
            t = _parse(m["game_time"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            mins_to_start = (t - now).total_seconds() / 60
            t_et = t.astimezone(et_tz)

            # Only today's games
            if t_et.date() != now.date():
                continue

            m["game_time_et"] = t_et.strftime("%-I:%M %p ET")
            m["mins_to_start"] = int(mins_to_start)

            if -5 <= mins_to_start <= 5:
                just_started.append(m)
            elif 25 <= mins_to_start <= 35:
                starting_soon.append(m)
        except Exception:
            continue

    # Fire once per matchup using Redis dedup
    def _already_alerted(key: str) -> bool:
        if r.get(f"alert:fired:{key}"):
            return True
        r.setex(f"alert:fired:{key}", 3600, "1")
        return False

    if starting_soon:
        unseen = [m for m in starting_soon if not _already_alerted(f"soon:{m['team']}:{m['game_time']}")]
        if unseen:
            send_games_starting_soon.delay(unseen)

    if just_started:
        unseen = [m for m in just_started if not _already_alerted(f"live:{m['team']}:{m['game_time']}")]
        if unseen:
            send_games_started.delay(unseen)
