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
    TICKERS,
    FIRST_CANDLE_READY_MINUTE,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
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
states: dict[str, StrategyState] = {t: StrategyState() for t in TICKERS}
client = MarketDataClient()


# ── Embed builders ──────────────────────────────────────────

def bias_embed(bias: DailyBias) -> discord.Embed:
    color = 0x00CC66 if bias.direction == "BULLISH" else 0xFF4444
    emoji = "\U0001f7e2" if bias.direction == "BULLISH" else "\U0001f534"

    embed = discord.Embed(
        title=f"{emoji} Daily Bias: {bias.direction}",
        color=color,
    )
    embed.add_field(name="Ticker", value=bias.ticker, inline=True)
    embed.add_field(name="Close", value=f"${bias.current_price}", inline=True)
    embed.add_field(name="50 SMA", value=f"${bias.sma_value}", inline=True)
    embed.add_field(name="Distance", value=f"{bias.distance_pct}%", inline=True)
    embed.add_field(name="Strength", value=bias.strength, inline=True)
    embed.set_footer(text="Based on 50-period SMA on the daily timeframe")
    return embed


def range_embed(fc: FirstCandleRange) -> discord.Embed:
    embed = discord.Embed(
        title=f"\U0001f4e6 First 5-Min Candle Range — {fc.ticker}",
        color=0x3498DB,
    )
    embed.add_field(name="Ticker", value=fc.ticker, inline=True)
    embed.add_field(name="High (Resistance)", value=f"${fc.high}", inline=True)
    embed.add_field(name="Low (Support)", value=f"${fc.low}", inline=True)
    embed.add_field(name="Range Size", value=f"${fc.range_size:.2f}", inline=True)
    embed.add_field(name="Candle Type", value=fc.candle_type, inline=True)
    embed.set_footer(text="9:30 - 9:35 AM ET | Do NOT trade inside this range")
    return embed


def signal_embed(signal: TradeSignal) -> discord.Embed:
    color = 0x00CC66 if signal.direction == "LONG" else 0xFF4444
    emoji = "\U0001f7e2" if signal.direction == "LONG" else "\U0001f534"
    arrow = "⬆️" if signal.direction == "LONG" else "⬇️"

    embed = discord.Embed(
        title=f"{emoji} {arrow} TRADE ALERT: {signal.direction} {signal.ticker}",
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
    logger.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Config: tickers=%s channel=%s poll=%ds window=%02d:%02d-%02d:%02d ET",
                ",".join(TICKERS), ALERT_CHANNEL_ID, POLL_INTERVAL_SECONDS,
                MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, MONITORING_END_HOUR, MONITORING_END_MINUTE)
    logger.info("Data sources: TwelveData=%s Finnhub=%s yfinance=always",
                "configured" if client.twelvedata_limiter else "not set",
                "configured" if client.finnhub_limiter else "not set")
    if not market_monitor.is_running():
        market_monitor.start()
        logger.info("Market monitor loop started (every %ds)", POLL_INTERVAL_SECONDS)


# ── Helpers ────────────────────────────────────────────────

def _resolve_ticker(arg: str | None) -> str | None:
    """Resolve a user-provided ticker argument to a tracked ticker."""
    if arg is None:
        return TICKERS[0] if len(TICKERS) == 1 else None
    upper = arg.upper()
    return upper if upper in states else None


# ── Commands ────────────────────────────────────────────────

@bot.command(name="bias")
async def cmd_bias(ctx, ticker: str = None):
    """Get today's daily bias. Usage: !bias [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!bias <{'|'.join(TICKERS)}>`")
        return
    st = states[resolved]
    if st.first_candle is None:
        await ctx.send(f"First 5-min candle hasn't been set for {resolved} yet. Use `!range {resolved}` or wait until after 9:35 AM ET.")
        return
    await ctx.send(f"Fetching daily bias for {resolved}...")
    try:
        bias = await get_daily_bias(client, resolved)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if bias is None:
        await ctx.send("Could not fetch daily data. Market may be closed.")
        return
    st.daily_bias = bias
    await ctx.send(embed=bias_embed(bias))


@bot.command(name="range")
async def cmd_range(ctx, ticker: str = None):
    """Get today's first 5-minute candle range, then fetch daily bias. Usage: !range [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!range <{'|'.join(TICKERS)}>`")
        return
    st = states[resolved]
    await ctx.send(f"Fetching first candle range for {resolved}...")
    try:
        fc = await get_first_candle_range(client, resolved)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if fc is None:
        await ctx.send("First 5-min candle not available yet. Wait until after 9:35 AM ET.")
        return
    st.first_candle = fc
    await ctx.send(embed=range_embed(fc))

    if st.daily_bias is None:
        try:
            bias = await get_daily_bias(client, resolved)
        except Exception as exc:
            await ctx.send(f"Error fetching daily bias: {exc}")
            return
        if bias is None:
            await ctx.send("Could not fetch daily bias data.")
            return
        st.daily_bias = bias
        await ctx.send(embed=bias_embed(bias))


@bot.command(name="status")
async def cmd_status(ctx):
    """Show current bot status for all tickers."""
    now = datetime.now(ET)
    market = "OPEN" if is_market_open() else "CLOSED"
    monitoring = "Active" if market_monitor.is_running() else "Idle"

    embed = discord.Embed(title="⚙️ Bot Status", color=0x95A5A6)
    embed.add_field(name="Market", value=market, inline=True)
    embed.add_field(name="Monitoring", value=monitoring, inline=True)
    embed.add_field(name="Time (ET)", value=now.strftime("%I:%M %p"), inline=True)
    embed.add_field(name="Tickers", value=", ".join(TICKERS), inline=True)

    for ticker in TICKERS:
        st = states[ticker]
        bias_str = st.daily_bias.direction if st.daily_bias else "Not set"
        candle_str = f"${st.first_candle.high} / ${st.first_candle.low}" if st.first_candle else "Not set"
        signal_str = "Yes" if st.signal_fired else "No"
        embed.add_field(
            name=f"── {ticker} ──",
            value=f"Bias: {bias_str}\nCandle: {candle_str}\nSignal: {signal_str}",
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
    time_str = now.strftime("%Y-%m-%d %I:%M:%S %p ET")

    if now.weekday() >= 5:
        logger.debug("[%s] Weekend — skipping", time_str)
        return

    today_str = now.strftime("%Y-%m-%d")
    channel = bot.get_channel(ALERT_CHANNEL_ID)

    # Reset state when the trading day changes (check first ticker as proxy)
    first_state = states[TICKERS[0]]
    if first_state.trading_day != today_str:
        logger.info("[%s] New trading day detected (%s) — resetting state for all tickers", time_str, today_str)
        for ticker in TICKERS:
            states[ticker].reset(today_str)
        if channel:
            market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
            end_time = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
            await channel.send(
                f"**Monitoring started for {today_str}** | "
                f"{', '.join(TICKERS)} | {market_open_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')} ET"
            )
        else:
            logger.error("[%s] Channel %s not found — cannot send messages", time_str, ALERT_CHANNEL_ID)

    # Stop monitoring after the cutoff
    if now.hour > MONITORING_END_HOUR or (
        now.hour == MONITORING_END_HOUR and now.minute >= MONITORING_END_MINUTE
    ):
        if not first_state.monitoring_ended:
            logger.info("[%s] Monitoring window ended", time_str)
            if channel:
                for ticker in TICKERS:
                    states[ticker].monitoring_ended = True
                fired = [t for t in TICKERS if states[t].signal_fired]
                if fired:
                    await channel.send(f"**Monitoring ended for {today_str}** | Signals fired: {', '.join(fired)}")
                else:
                    await channel.send(f"**Monitoring ended for {today_str}** | No signals today.")
        return

    if channel is None:
        logger.error("[%s] Channel %s not found — skipping poll", time_str, ALERT_CHANNEL_ID)
        return

    candle_ready_time = now.replace(hour=MARKET_OPEN_HOUR, minute=FIRST_CANDLE_READY_MINUTE, second=0, microsecond=0)
    past_first_candle = now >= candle_ready_time

    if not past_first_candle:
        logger.info("[%s] Waiting for first candle (ready at %s)", time_str, candle_ready_time.strftime("%I:%M %p"))
        return

    for ticker in TICKERS:
        st = states[ticker]

        if st.signal_fired:
            continue

        logger.info("[%s] [%s] Poll cycle | first_candle=%s | bias=%s | broke_above=%s | broke_below=%s",
                    time_str, ticker,
                    f"${st.first_candle.high}/${st.first_candle.low}" if st.first_candle else "None",
                    st.daily_bias.direction if st.daily_bias else "None",
                    st.broke_above,
                    st.broke_below)

        try:
            # Step 1: Get first candle range after 9:35 AM ET
            if st.first_candle is None:
                logger.info("[%s] [%s] Fetching first 5-min candle...", time_str, ticker)
                fc = await get_first_candle_range(client, ticker)
                if fc:
                    st.first_candle = fc
                    logger.info("[%s] [%s] First candle set: high=$%s low=$%s range=$%.2f type=%s",
                                time_str, ticker, fc.high, fc.low, fc.range_size, fc.candle_type)
                    await channel.send(embed=range_embed(fc))
                else:
                    logger.warning("[%s] [%s] First candle returned None — will retry next poll", time_str, ticker)

            # Step 2: Get daily bias after the first 5-min candle is posted
            if st.first_candle is not None and st.daily_bias is None:
                logger.info("[%s] [%s] Fetching daily bias...", time_str, ticker)
                bias = await get_daily_bias(client, ticker)
                if bias:
                    st.daily_bias = bias
                    logger.info("[%s] [%s] Daily bias set: %s (close=$%s sma=$%s dist=%s%% strength=%s)",
                                time_str, ticker, bias.direction, bias.current_price, bias.sma_value,
                                bias.distance_pct, bias.strength)
                    await channel.send(embed=bias_embed(bias))
                else:
                    logger.warning("[%s] [%s] Daily bias returned None — will retry next poll", time_str, ticker)

            # Step 3: Check for trade setups
            if st.daily_bias and st.first_candle and not st.signal_fired:
                signal = await check_for_setup(st, client, ticker)
                if signal:
                    logger.info("[%s] [%s] SIGNAL FIRED: %s entry=$%s stop=$%s target=$%s",
                                time_str, ticker, signal.direction, signal.entry_price, signal.stop_loss, signal.target)
                    await channel.send("@everyone", embed=signal_embed(signal))
                else:
                    logger.debug("[%s] [%s] No setup detected this poll", time_str, ticker)

        except Exception as exc:
            logger.exception("[%s] [%s] Monitor error: %s", time_str, ticker, exc)


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
