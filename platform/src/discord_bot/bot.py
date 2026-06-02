"""
Discord Bot — Sports Intelligence Platform.

Manages 20 channels, 10 slash commands, alert routing, and AI discussion mode.
"""
import logging
import discord
from discord.ext import commands
from src.core.config import DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_CHANNELS

logger = logging.getLogger(__name__)

CHANNEL_NAMES = list(DISCORD_CHANNELS.keys())

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Startup ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("Discord bot logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash commands", len(synced))
    except Exception as e:
        logger.error("Failed to sync slash commands: %s", e)


# ── Helper ─────────────────────────────────────────────────────────────────────

async def get_channel(name: str) -> discord.TextChannel | None:
    """Resolve a channel by configured name."""
    channel_id = DISCORD_CHANNELS.get(name)
    if channel_id:
        return bot.get_channel(channel_id)
    guild = bot.get_guild(DISCORD_GUILD_ID)
    if not guild:
        return None
    return discord.utils.get(guild.text_channels, name=name.lstrip("#"))


async def send_to_channel(channel_name: str, content: str = "", embed: discord.Embed | None = None) -> bool:
    """Send a message or embed to a named channel."""
    ch = await get_channel(channel_name)
    if not ch:
        logger.warning("Channel not found: %s", channel_name)
        return False
    try:
        if embed:
            await ch.send(content=content or None, embed=embed)
        else:
            await ch.send(content)
        return True
    except discord.DiscordException as e:
        logger.error("Failed to send to %s: %s", channel_name, e)
        return False


# ── Slash Commands ─────────────────────────────────────────────────────────────

MAX_ANALYZE_CHARS = 500


@bot.tree.command(name="analyze", description="Analyse a betting situation or game")
async def cmd_analyze(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    # Truncate to prevent token abuse
    query = query[:MAX_ANALYZE_CHARS]
    from src.engines.ai_engine import handle_command
    result = handle_command("analyze", query)
    embed = discord.Embed(title="Analysis", description=result, color=0x1E88E5)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="player", description="Player stats, props, and injury analysis")
async def cmd_player(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("player", player)
    embed = discord.Embed(title=f"Player: {player}", description=result, color=0x43A047)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="team", description="Team form, trends, and matchup analysis")
async def cmd_team(interaction: discord.Interaction, team: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("team", team)
    embed = discord.Embed(title=f"Team: {team}", description=result, color=0x8E24AA)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="parlay", description="Evaluate a parlay for value and correlation")
async def cmd_parlay(interaction: discord.Interaction, legs: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("parlay", legs)
    embed = discord.Embed(title="Parlay Evaluation", description=result, color=0xFDD835)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="explain", description="Explain a betting concept in plain English")
async def cmd_explain(interaction: discord.Interaction, concept: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("explain", concept)
    embed = discord.Embed(title=f"Explained: {concept}", description=result, color=0x00ACC1)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="why", description="Why was a bet recommended or why did it win/lose?")
async def cmd_why(interaction: discord.Interaction, bet: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("why", bet)
    embed = discord.Embed(title="Why?", description=result, color=0xFF7043)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="odds", description="Explain odds and calculate EV")
async def cmd_odds(interaction: discord.Interaction, odds: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("odds", odds)
    embed = discord.Embed(title="Odds Analysis", description=result, color=0x6D4C41)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="results", description="View recent betting results")
async def cmd_results(interaction: discord.Interaction, period: str = "weekly"):
    await interaction.response.defer()
    from src.engines.portfolio_engine import get_performance_stats
    stats = get_performance_stats(period)
    desc = (
        f"**Period:** {period.title()}\n"
        f"**Record:** {stats['wins']}W - {stats['losses']}L\n"
        f"**Net Units:** {stats['net_units']:+.2f}u\n"
        f"**ROI:** {stats['roi']:.1%}\n"
        f"**Hit Rate:** {stats['hit_rate']:.1%}\n"
        f"**Profit Factor:** {stats.get('profit_factor', 0):.2f}"
    )
    embed = discord.Embed(title=f"Results ({period.title()})", description=desc, color=0x2E7D32 if stats['net_units'] >= 0 else 0xC62828)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="bankroll", description="Bankroll management advice")
async def cmd_bankroll(interaction: discord.Interaction, situation: str):
    await interaction.response.defer()
    from src.engines.ai_engine import handle_command
    result = handle_command("bankroll", situation)
    embed = discord.Embed(title="Bankroll Advice", description=result, color=0x00897B)
    await interaction.followup.send(embed=embed)


MAX_BANKROLL = 10_000_000.0


@bot.tree.command(name="setbankroll", description="Set your bankroll amount")
async def cmd_setbankroll(interaction: discord.Interaction, amount: float):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        await interaction.followup.send(
            "Invalid bankroll: amount must be greater than $0.", ephemeral=True
        )
        return
    if amount > MAX_BANKROLL:
        await interaction.followup.send(
            f"Invalid bankroll: amount must not exceed ${MAX_BANKROLL:,.0f}.", ephemeral=True
        )
        return
    from src.engines.personalization_engine import get_profile, save_profile
    profile = get_profile(str(interaction.user.id))
    profile.bankroll = amount
    save_profile(profile)
    await interaction.followup.send(f"Bankroll set to **${amount:,.2f}**", ephemeral=True)


@bot.tree.command(name="setsports", description="Set your sports (comma-separated)")
async def cmd_setsports(interaction: discord.Interaction, sports: str):
    await interaction.response.defer(ephemeral=True)
    from src.engines.personalization_engine import get_profile, save_profile
    profile = get_profile(str(interaction.user.id))
    profile.sports = [s.strip() for s in sports.split(",") if s.strip()]
    save_profile(profile)
    await interaction.followup.send(f"Sports set to: **{', '.join(profile.sports)}**", ephemeral=True)


@bot.tree.command(name="setrisk", description="Set risk profile: conservative, balanced, or aggressive")
async def cmd_setrisk(interaction: discord.Interaction, profile: str):
    await interaction.response.defer(ephemeral=True)
    from src.engines.personalization_engine import get_profile, save_profile, RISK_PROFILES
    if profile not in RISK_PROFILES:
        await interaction.followup.send("Invalid profile. Choose: conservative, balanced, aggressive", ephemeral=True)
        return
    user_profile = get_profile(str(interaction.user.id))
    user_profile.risk_profile = profile
    user_profile.max_units = RISK_PROFILES[profile]["max_units"]
    user_profile.min_ev = RISK_PROFILES[profile]["min_ev"]
    save_profile(user_profile)
    await interaction.followup.send(f"Risk profile set to **{profile}**", ephemeral=True)


@bot.tree.command(name="profile", description="Show your current settings")
async def cmd_profile(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from src.engines.personalization_engine import get_profile
    p = get_profile(str(interaction.user.id))
    desc = (
        f"**Bankroll:** ${p.bankroll:,.2f}\n"
        f"**Risk Profile:** {p.risk_profile}\n"
        f"**Max Units:** {p.max_units}\n"
        f"**Min EV:** {p.min_ev:.1%}\n"
        f"**Sports:** {', '.join(p.sports) if p.sports else 'All'}"
    )
    embed = discord.Embed(title="Your Profile", description=desc, color=0x00897B)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="rate", description="Rate a pick: /rate <pick_id> <helpful|not_helpful>")
async def cmd_rate(interaction: discord.Interaction, pick_id: int, rating: str):
    await interaction.response.defer(ephemeral=True)
    from src.engines.feedback_engine import submit_feedback, PickFeedback
    if rating not in ("helpful", "not_helpful"):
        await interaction.followup.send("Rating must be 'helpful' or 'not_helpful'", ephemeral=True)
        return
    fb = PickFeedback(pick_id=pick_id, user_id=str(interaction.user.id), rating=rating, explanation_rating=3)
    submit_feedback(fb)
    await interaction.followup.send(f"Feedback submitted for pick #{pick_id}: **{rating}**", ephemeral=True)


@bot.tree.command(name="health", description="Show system health dashboard")
async def cmd_health(interaction: discord.Interaction):
    await interaction.response.defer()
    from src.engines.health_engine import get_system_health, format_health_report
    health = get_system_health()
    report = format_health_report(health)
    color = {"ok": 0x2E7D32, "degraded": 0xF57F17, "down": 0xC62828}.get(health.overall, 0x757575)
    embed = discord.Embed(title="System Health", description=report, color=color)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="top-picks", description="Best picks available right now")
async def cmd_top_picks(interaction: discord.Interaction):
    await interaction.response.defer()
    from src.engines.self_improvement_engine import get_top_performing_categories
    from src.engines.ai_engine import handle_command
    top = get_top_performing_categories(limit=5)
    context = {"top_performing": top}
    result = handle_command("top-picks", "today's top opportunities", context=context)
    embed = discord.Embed(title="Top Picks", description=result, color=0xE53935)
    await interaction.followup.send(embed=embed)


# ── Alert Embeds ───────────────────────────────────────────────────────────────

def _pick_embed(pick: dict) -> discord.Embed:
    rec = pick.get("recommendation", "PASS")
    color = 0x2E7D32 if rec == "BET" else 0x757575
    units = pick.get("units", 0)
    unit_bar = "█" * units + "░" * (5 - units)

    embed = discord.Embed(
        title=f"[{rec}] {pick.get('bet', 'Unknown')}",
        description=pick.get("reasoning", ""),
        color=color,
    )
    embed.add_field(name="Game", value=pick.get("game", "—"), inline=True)
    embed.add_field(name="Sport", value=pick.get("sport", "—"), inline=True)
    embed.add_field(name="Odds", value=f"{pick.get('odds', 0):+d}", inline=True)
    embed.add_field(name="Units", value=f"{unit_bar} ({units}/5)", inline=True)
    embed.add_field(name="EV", value=f"{pick.get('ev_pct', 0):.1%}", inline=True)
    embed.add_field(name="Confidence", value=f"{pick.get('confidence_pct', 0):.0f}%", inline=True)
    embed.add_field(name="Best Book", value=pick.get("best_book", "—"), inline=True)
    embed.add_field(name="CLV Potential", value=f"{pick.get('clv_potential', 0):.1%}", inline=True)

    br = pick.get("bankroll_examples", {})
    if br:
        br_text = "\n".join(
            f"${k}: stake ${v.get('stake',0):.0f} → win ${v.get('profit',0):.0f}"
            for k, v in sorted(br.items())
        )
        embed.add_field(name="Bankroll Examples", value=br_text, inline=False)

    key_factors = pick.get("key_factors", [])
    if key_factors:
        embed.add_field(name="Key Factors", value="\n".join(f"• {f}" for f in key_factors[:3]), inline=False)

    risk_flags = pick.get("risk_flags", [])
    if risk_flags:
        embed.add_field(name="Risk Flags", value="\n".join(f"• {f}" for f in risk_flags[:3]), inline=False)

    embed.set_footer(text=pick.get("unit_reason", ""))
    return embed


def _result_embed(pick: dict, result: str) -> discord.Embed:
    colors = {"won": 0x2E7D32, "lost": 0xC62828, "push": 0xF57F17}
    labels = {"won": "WIN", "lost": "LOSS", "push": "PUSH"}
    pnl = pick.get("actual_pnl_units", 0)
    embed = discord.Embed(
        title=f"[{labels.get(result, result.upper())}] {pick.get('bet', 'Unknown')}",
        description=f"P&L: **{pnl:+.2f}u**",
        color=colors.get(result, 0x757575),
    )
    embed.add_field(name="Game", value=pick.get("game", "—"), inline=True)
    embed.add_field(name="Sport", value=pick.get("sport", "—"), inline=True)
    embed.add_field(name="Odds", value=f"{pick.get('odds', 0):+d}", inline=True)
    clv = pick.get("clv_pct")
    if clv is not None:
        embed.add_field(name="CLV", value=f"{clv:+.2%}", inline=True)
    return embed


# ── Public API used by alert router ───────────────────────────────────────────

async def post_pick(pick: dict) -> None:
    sport = pick.get("sport", "").lower()
    channel = SPORT_TO_CHANNEL.get(sport, "top-picks")
    embed = _pick_embed(pick)
    await send_to_channel("top-picks", embed=embed)
    if channel != "top-picks":
        await send_to_channel(channel, embed=embed)


async def post_result(pick: dict, result: str) -> None:
    embed = _result_embed(pick, result)
    await send_to_channel("results", embed=embed)


async def post_line_movement(movement: dict) -> None:
    embed = discord.Embed(
        title=f"Line Movement: {movement.get('game', '')}",
        description=movement.get("description", ""),
        color=0xFDD835,
    )
    embed.add_field(name="Type", value=movement.get("move_type", "—"), inline=True)
    embed.add_field(name="Move", value=f"{movement.get('move_size', 0):+.1f}", inline=True)
    await send_to_channel("line-movement", embed=embed)


async def post_parlay(parlay: dict) -> None:
    legs = parlay.get("legs", [])
    desc = "\n".join(f"• {l.get('bet', l)}" for l in legs)
    embed = discord.Embed(
        title=f"Parlay ({len(legs)} legs) — {parlay.get('combined_odds', 0):+d}",
        description=desc,
        color=0xFDD835,
    )
    embed.add_field(name="Combined EV", value=f"{parlay.get('combined_ev', 0):.1%}", inline=True)
    embed.add_field(name="Units", value=str(parlay.get("units", 0)), inline=True)
    await send_to_channel("parlays", embed=embed)


async def post_daily_summary(summary_text: str) -> None:
    embed = discord.Embed(title="Daily Summary", description=summary_text, color=0x1565C0)
    await send_to_channel("daily-summary", embed=embed)


async def post_weekly_summary(summary_text: str) -> None:
    embed = discord.Embed(title="Weekly Summary", description=summary_text, color=0x4A148C)
    await send_to_channel("weekly-summary", embed=embed)


async def post_game_alert(event: dict, minutes_remaining: float) -> None:
    urgency_colors = {"critical": 0xC62828, "high": 0xF57F17, "medium": 0xFDD835, "low": 0x1E88E5}
    from src.engines.timing_engine import get_urgency_level
    urgency = get_urgency_level(minutes_remaining)
    embed = discord.Embed(
        title=f"Game Alert: {event.get('home_team', '')} vs {event.get('away_team', '')}",
        description=f"Starting in **{int(minutes_remaining)} minutes**",
        color=urgency_colors.get(urgency, 0x757575),
    )
    embed.add_field(name="Sport", value=event.get("sport", "—"), inline=True)
    embed.add_field(name="Urgency", value=urgency.upper(), inline=True)
    await send_to_channel("game-alerts", embed=embed)


SPORT_TO_CHANNEL = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "soccer",
    "soccer_uefa_champs_league": "soccer",
    "mma": "mma",
    "tennis": "tennis",
    "golf": "golf",
    "esports": "esports",
}


def run_bot():
    from src.core.config import DISCORD_BOT_TOKEN
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set — bot will not start")
        return
    bot.run(DISCORD_BOT_TOKEN)
