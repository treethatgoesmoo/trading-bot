import asyncio
import logging
import discord
from discord.ext import commands, tasks
from datetime import datetime
import pytz

from config import (
    DISCORD_TOKEN,
    ALERT_CHANNEL_ID,
    COMMAND_PREFIX,
    TICKER,
    FIRST_CANDLE_READY_MINUTE,
    MARKET_OPEN_HOUR,
    MONITORING_END_HOUR,
    MONITORING_END_MINUTE,
    POLL_INTERVAL_SECONDS,
)
from strategy import (
    get_daily_bias,
    get_first_candle_range,
    check_for_setup,
    StrategyState,
    DailyBias,
    FirstCandleRange,
    TradeSignal,
)
from market_data import MarketDataClient, is_market_open

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
state = StrategyState()
client = MarketDataClient()


# ── Embed builders ──────────────────────────────────────────

def bias_embed(bias: DailyBias) -> discord.Embed:
    color = 0x00CC66 if bias.direction == "BULLISH" else 0xFF4444
    emoji = "\U0001f7e2" if bias.direction == "BULLISH" else "\U0001f534"

    embed = discord.Embed(
        title=f"{emoji} Daily Bias: {bias.direction}",
        color=color,
    )
    embed.add_field(name="Ticker", value=TICKER, inline=True)
    embed.add_field(name="Close", value=f"${bias.current_price}", inline=True)
    embed.add_field(name="50 SMA", value=f"${bias.sma_value}", inline=True)
    embed.add_field(name="Distance", value=f"{bias.distance_pct}%", inline=True)
    embed.add_field(name="Strength", value=bias.strength, inline=True)
    embed.set_footer(text="Based on 50-period SMA on the daily timeframe")
    return embed


def range_embed(fc: FirstCandleRange) -> discord.Embed:
    embed = discord.Embed(
        title="\U0001f4e6 First 5-Min Candle Range",
        color=0x3498DB,
    )
    embed.add_field(name="Ticker", value=TICKER, inline=True)
    embed.add_field(name="High (Resistance)", value=f"${fc.high}", inline=True)
    embed.add_field(name="Low (Support)", value=f"${fc.low}", inline=True)
    embed.add_field(name="Range Size", value=f"${fc.range_size:.2f}", inline=True)
    embed.add_field(name="Candle Type", value=fc.candle_type, inline=True)
    embed.set_footer(text="9:30 - 9:35 AM ET | Do NOT trade inside this range")
    return embed


def signal_embed(signal: TradeSignal) -> discord.Embed:
    color = 0x00CC66 if signal.direction == "LONG" else 0xFF4444
    emoji = "\U0001f7e2" if signal.direction == "LONG" else "\U0001f534"
    arrow = "\u2B06\uFE0F" if signal.direction == "LONG" else "\u2B07\uFE0F"

    embed = discord.Embed(
        title=f"{emoji} {arrow} TRADE ALERT: {signal.direction} {TICKER}",
        color=color,
    )
    embed.add_field(name="Entry", value=f"${signal.entry_price}", inline=True)
    embed.add_field(name="Stop Loss", value=f"${signal.stop_loss}", inline=True)
    embed.add_field(name="Target (2R)", value=f"${signal.target}", inline=True)
    embed.add_field(name="Risk", value=f"${signal.risk}", inline=True)
    embed.add_field(name="Reward", value=f"${signal.reward}", inline=True)
    embed.add_field(name="R:R", value=f"1:{int(signal.reward / signal.risk) if signal.risk else 0}", inline=True)
    embed.set_footer(text=f"Signal at {signal.timestamp} | First Candle Strategy")
    return embed


# ── Bot events ──────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("Bot connected as %s", bot.user)
    if not market_monitor.is_running():
        market_monitor.start()


# ── Commands ────────────────────────────────────────────────

@bot.command(name="bias")
async def cmd_bias(ctx):
    """Get today's daily bias (bullish/bearish)."""
    await ctx.send("Fetching daily bias...")
    try:
        bias = await get_daily_bias(client)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if bias is None:
        await ctx.send("Could not fetch daily data. Market may be closed.")
        return
    state.daily_bias = bias
    await ctx.send(embed=bias_embed(bias))


@bot.command(name="range")
async def cmd_range(ctx):
    """Get today's first 5-minute candle range."""
    await ctx.send("Fetching first candle range...")
    try:
        fc = await get_first_candle_range(client)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if fc is None:
        await ctx.send("First 5-min candle not available yet. Wait until after 9:35 AM ET.")
        return
    state.first_candle = fc
    await ctx.send(embed=range_embed(fc))


@bot.command(name="status")
async def cmd_status(ctx):
    """Show current bot status."""
    now = datetime.now(ET)
    market = "OPEN" if is_market_open() else "CLOSED"
    monitoring = "Active" if market_monitor.is_running() and not state.signal_fired else "Idle"

    embed = discord.Embed(title="\u2699\uFE0F Bot Status", color=0x95A5A6)
    embed.add_field(name="Market", value=market, inline=True)
    embed.add_field(name="Monitoring", value=monitoring, inline=True)
    embed.add_field(name="Time (ET)", value=now.strftime("%I:%M %p"), inline=True)
    embed.add_field(
        name="Daily Bias",
        value=state.daily_bias.direction if state.daily_bias else "Not set",
        inline=True,
    )
    embed.add_field(
        name="First Candle",
        value=f"${state.first_candle.high} / ${state.first_candle.low}" if state.first_candle else "Not set",
        inline=True,
    )
    embed.add_field(
        name="Signal Fired",
        value="Yes" if state.signal_fired else "No",
        inline=True,
    )

    # Show data source status
    sources = []
    if client.twelvedata_limiter:
        remaining = client.twelvedata_limiter.remaining_daily
        sources.append(f"TwelveData: {remaining} calls left")
    else:
        sources.append("TwelveData: not configured")
    if client.finnhub_limiter:
        sources.append("Finnhub: active")
    else:
        sources.append("Finnhub: not configured")
    sources.append("yfinance: fallback ready")
    embed.add_field(name="Data Sources", value="\n".join(sources), inline=False)

    await ctx.send(embed=embed)


# ── Background monitor ──────────────────────────────────────

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def market_monitor():
    """Main loop that runs during market hours."""
    now = datetime.now(ET)

    if now.weekday() >= 5:
        return

    # Reset state at the start of each trading day
    if now.hour == MARKET_OPEN_HOUR and now.minute == MARKET_OPEN_MINUTE and not state.first_candle:
        state.reset()

    # Stop monitoring after the cutoff
    if now.hour > MONITORING_END_HOUR or (
        now.hour == MONITORING_END_HOUR and now.minute >= MONITORING_END_MINUTE
    ):
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        return

    try:
        # Step 1: Get first candle range after 9:35 AM ET
        if state.first_candle is None:
            if now.hour == MARKET_OPEN_HOUR and now.minute >= FIRST_CANDLE_READY_MINUTE:
                fc = await get_first_candle_range(client)
                if fc:
                    state.first_candle = fc
                    await channel.send(embed=range_embed(fc))

        # Step 2: Get daily bias after the first 5-min candle is posted
        if state.first_candle is not None and state.daily_bias is None:
            bias = await get_daily_bias(client)
            if bias:
                state.daily_bias = bias
                await channel.send(embed=bias_embed(bias))

        # Step 3: Check for trade setups
        if state.daily_bias and state.first_candle and not state.signal_fired:
            if now.hour >= MARKET_OPEN_HOUR and now.minute >= FIRST_CANDLE_READY_MINUTE:
                signal = await check_for_setup(state, client)
                if signal:
                    await channel.send("@everyone", embed=signal_embed(signal))

    except Exception as exc:
        logger.exception("Monitor error: %s", exc)


@market_monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()


async def run_bot():
    """Start the Discord bot."""
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env file")
        print("1. Create a bot at https://discord.com/developers/applications")
        print("2. Copy the token into your .env file")
        return

    async with bot:
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await client.close()
