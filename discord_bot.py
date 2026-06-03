import asyncio
import json
import logging
import os
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
    ATR_OVEREXTENDED_MULT,
    FILTER_COOLDOWN_MINUTES,
    TRADE_TRACKING_END_HOUR,
    TRADE_TRACKING_END_MINUTE,
)
from strategy import (
    get_daily_bias,
    get_first_candle_range,
    get_premarket_levels,
    check_for_setup,
    check_trade_outcome,
    compute_atr,
    StrategyState,
    DailyBias,
    FirstCandleRange,
    PremarketLevels,
    TradeSignal,
    FilteredSignal,
    TradeOutcome,
)
from market_data import MarketDataClient, is_market_open

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), ".watchlist.json")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
active_tickers: list[str] = list(TICKERS)
states: dict[str, StrategyState] = {t: StrategyState() for t in active_tickers}
client = MarketDataClient()


def _load_watchlist():
    """Load saved watchlist and merge with env tickers."""
    global active_tickers, states
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                saved = json.load(f)
            for t in saved:
                if t not in active_tickers:
                    active_tickers.append(t)
                    states[t] = StrategyState()
        except Exception:
            pass


def _save_watchlist():
    """Persist current watchlist to disk."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(active_tickers, f)
    except Exception:
        pass


_load_watchlist()


# ── Embed builders ──────────────────────────────────────────

def bias_embed(bias: DailyBias) -> discord.Embed:
    color = 0x00CC66 if bias.direction == "BULLISH" else 0xFF4444
    emoji = "\U0001f7e2" if bias.direction == "BULLISH" else "\U0001f534"
    trend_emoji = "\U0001f7e2" if bias.trend == "BULLISH" else "\U0001f534"
    prev_emoji = "\U0001f7e2" if bias.short_term == "BULLISH" else "\U0001f534"
    aligned = "Yes" if bias.aligned else "No"

    embed = discord.Embed(title=f"{emoji} Daily Bias: {bias.direction}", color=color)
    embed.add_field(name="Ticker", value=bias.ticker, inline=True)
    embed.add_field(name="Prev Day", value=f"{prev_emoji} {bias.short_term} (${bias.prev_open} -> ${bias.prev_close})", inline=True)
    embed.add_field(name="50 SMA Trend", value=f"{trend_emoji} {bias.trend} (${bias.current_price} vs ${bias.sma_value})", inline=True)
    embed.add_field(name="Distance", value=f"{bias.distance_pct}%", inline=True)
    embed.add_field(name="Aligned", value=aligned, inline=True)
    embed.set_footer(text="Bias based on previous day candle direction | Trend from 50 SMA")
    return embed


def range_embed(fc: FirstCandleRange) -> discord.Embed:
    embed = discord.Embed(title=f"\U0001f4e6 First 5-Min Candle Range — {fc.ticker}", color=0x3498DB)
    embed.add_field(name="Ticker", value=fc.ticker, inline=True)
    embed.add_field(name="High (Resistance)", value=f"${fc.high}", inline=True)
    embed.add_field(name="Low (Support)", value=f"${fc.low}", inline=True)
    embed.add_field(name="Range Size", value=f"${fc.range_size:.2f}", inline=True)
    embed.add_field(name="Candle Type", value=fc.candle_type, inline=True)
    embed.set_footer(text="9:30 - 9:35 AM ET | Do NOT trade inside this range")
    return embed


def premarket_embed(pm: PremarketLevels) -> discord.Embed:
    embed = discord.Embed(title=f"\U0001f4ca Pre-Market Levels — {pm.ticker}", color=0x9B59B6)
    embed.add_field(name="PM High", value=f"${pm.high}", inline=True)
    embed.add_field(name="PM Low", value=f"${pm.low}", inline=True)
    embed.add_field(name="PM Volume", value=f"{pm.volume:,.0f}", inline=True)
    embed.set_footer(text="Pre-market session (4:00 AM - 9:30 AM ET)")
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
    if signal.filters:
        filter_lines = [f"✅ {f.name}: {f.reason}" for f in signal.filters]
        embed.add_field(name="Filters", value="\n".join(filter_lines), inline=False)
    embed.set_footer(text=f"Signal at {signal.timestamp} | First Candle Strategy")
    return embed


def filtered_embed(fs: FilteredSignal) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ Setup Filtered: {fs.direction} {fs.ticker}",
        color=0xFFA500,
    )
    embed.add_field(name="Entry (would be)", value=f"${fs.entry_price}", inline=True)
    embed.add_field(name="Time", value=fs.timestamp, inline=True)
    embed.add_field(name="Cooldown", value=f"{FILTER_COOLDOWN_MINUTES} min", inline=True)
    lines = []
    for f in fs.filters:
        icon = "✅" if f.passed else "❌"
        lines.append(f"{icon} **{f.name}**: {f.reason}")
    embed.add_field(name="Filter Results", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Will retry after {FILTER_COOLDOWN_MINUTES} min cooldown")
    return embed


def overextended_embed(ticker: str, range_size: float, atr: float) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ {ticker} — Opening Range Overextended",
        color=0xFFA500,
    )
    embed.add_field(name="First Candle Range", value=f"${range_size:.2f}", inline=True)
    embed.add_field(name="ATR (14-period)", value=f"${atr:.2f}", inline=True)
    embed.add_field(name="Threshold", value=f"{ATR_OVEREXTENDED_MULT}x ATR = ${atr * ATR_OVEREXTENDED_MULT:.2f}", inline=True)
    embed.set_footer(text=f"Skipping {ticker} for today")
    return embed


def outcome_embed(outcome: TradeOutcome) -> discord.Embed:
    if outcome.result == "TARGET_HIT":
        color = 0x00CC66
        icon = "✅"
        title = f"{icon} TARGET HIT: {outcome.direction} {outcome.ticker}"
    else:
        color = 0xFF4444
        icon = "❌"
        title = f"{icon} STOPPED OUT: {outcome.direction} {outcome.ticker}"

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Entry", value=f"${outcome.entry_price}", inline=True)
    embed.add_field(name="Exit", value=f"${outcome.exit_price}", inline=True)
    embed.add_field(name="P/L", value=f"${outcome.pnl:+.2f}", inline=True)
    embed.add_field(name="Stop", value=f"${outcome.stop_loss}", inline=True)
    embed.add_field(name="Target", value=f"${outcome.target}", inline=True)
    embed.set_footer(text=f"Closed at {outcome.timestamp}")
    return embed


def daily_summary_embed(today_str: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"\U0001f4cb Daily Summary — {today_str}",
        color=0x2ECC71,
    )
    for ticker in active_tickers:
        st = states.get(ticker)
        if st is None:
            continue

        lines = []
        if st.daily_bias:
            lines.append(f"Bias: {st.daily_bias.direction} ({st.daily_bias.strength})")
        if st.first_candle:
            lines.append(f"Range: ${st.first_candle.high} / ${st.first_candle.low} (${st.first_candle.range_size:.2f})")
        if st.first_candle_overextended:
            lines.append("Skipped: Overextended opening")
        elif st.trade_outcome:
            o = st.trade_outcome
            icon = "✅" if o.result == "TARGET_HIT" else "❌"
            lines.append(f"{icon} {o.direction}: ${o.entry_price} -> ${o.exit_price} (${o.pnl:+.2f})")
        elif st.active_trade:
            lines.append(f"Open: {st.active_trade.direction} @ ${st.active_trade.entry_price}")
        elif st.signal_fired:
            lines.append("Signal fired (no outcome tracked)")
        else:
            lines.append("No signal")

        if st.filtered_signals:
            lines.append(f"Filtered setups: {len(st.filtered_signals)}")

        embed.add_field(
            name=f"── {ticker} ──",
            value="\n".join(lines) if lines else "No activity",
            inline=True,
        )

    return embed


# ── Bot events ──────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Config: tickers=%s channel=%s poll=%ds window=%02d:%02d-%02d:%02d ET",
                ",".join(active_tickers), ALERT_CHANNEL_ID, POLL_INTERVAL_SECONDS,
                MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, MONITORING_END_HOUR, MONITORING_END_MINUTE)
    logger.info("Data sources: TwelveData=%s Finnhub=%s yfinance=always",
                "configured" if client.twelvedata_limiter else "not set",
                "configured" if client.finnhub_limiter else "not set")
    if not market_monitor.is_running():
        market_monitor.start()
        logger.info("Market monitor loop started (every %ds)", POLL_INTERVAL_SECONDS)


# ── Helpers ────────────────────────────────────────────────

def _resolve_ticker(arg: str | None) -> str | None:
    if arg is None:
        return active_tickers[0] if len(active_tickers) == 1 else None
    upper = arg.upper()
    return upper if upper in states else None


# ── Commands ────────────────────────────────────────────────

@bot.command(name="bias")
async def cmd_bias(ctx, ticker: str = None):
    """Get today's daily bias. Usage: !bias [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!bias <{'|'.join(active_tickers)}>`")
        return
    st = states[resolved]
    if st.first_candle is None:
        await ctx.send(f"First 5-min candle hasn't been set for {resolved} yet.")
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
    """Get today's first 5-minute candle range. Usage: !range [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!range <{'|'.join(active_tickers)}>`")
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
    embed.add_field(name="Tickers", value=", ".join(active_tickers), inline=True)

    for ticker in active_tickers:
        st = states[ticker]
        bias_str = st.daily_bias.direction if st.daily_bias else "Not set"
        candle_str = f"${st.first_candle.high} / ${st.first_candle.low}" if st.first_candle else "Not set"
        atr_str = f"${st.atr_value:.2f}" if st.atr_value else "N/A"
        overext_str = " (OVEREXTENDED)" if st.first_candle_overextended else ""

        status_lines = [
            f"Bias: {bias_str}",
            f"Candle: {candle_str}",
            f"ATR: {atr_str}{overext_str}",
        ]

        if st.trade_outcome:
            o = st.trade_outcome
            icon = "✅" if o.result == "TARGET_HIT" else "❌"
            status_lines.append(f"Trade: {icon} {o.result} (${o.pnl:+.2f})")
        elif st.active_trade:
            status_lines.append(f"Trade: OPEN {st.active_trade.direction} @ ${st.active_trade.entry_price}")
        elif st.signal_fired:
            status_lines.append("Signal: Fired")
        else:
            status_lines.append("Signal: Waiting")

        if st.premarket:
            status_lines.append(f"PM: ${st.premarket.high} / ${st.premarket.low}")

        embed.add_field(name=f"── {ticker} ──", value="\n".join(status_lines), inline=True)

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


@bot.command(name="add")
async def cmd_add(ctx, ticker: str = None):
    """Add a ticker to the watchlist. Usage: !add AAPL"""
    if ticker is None:
        await ctx.send("Usage: `!add <TICKER>`")
        return
    upper = ticker.upper()
    if upper in states:
        await ctx.send(f"{upper} is already being tracked.")
        return
    active_tickers.append(upper)
    states[upper] = StrategyState()
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    states[upper].trading_day = today_str
    _save_watchlist()
    await ctx.send(f"✅ Added **{upper}** to watchlist. Now tracking: {', '.join(active_tickers)}")


@bot.command(name="remove")
async def cmd_remove(ctx, ticker: str = None):
    """Remove a ticker from the watchlist. Usage: !remove AAPL"""
    if ticker is None:
        await ctx.send("Usage: `!remove <TICKER>`")
        return
    upper = ticker.upper()
    if upper not in states:
        await ctx.send(f"{upper} is not being tracked.")
        return
    active_tickers.remove(upper)
    del states[upper]
    _save_watchlist()
    await ctx.send(f"❌ Removed **{upper}** from watchlist. Now tracking: {', '.join(active_tickers)}")


@bot.command(name="watchlist")
async def cmd_watchlist(ctx):
    """Show current watchlist."""
    await ctx.send(f"**Watchlist:** {', '.join(active_tickers)}")


@bot.command(name="test")
async def cmd_test(ctx, ticker: str = None):
    """Simulate the full strategy flow for a ticker. Usage: !test [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!test <{'|'.join(active_tickers)}>`")
        return

    await ctx.send(f"**Running full strategy test for {resolved}...**")

    # Step 0: Pre-market levels
    await ctx.send("Fetching pre-market levels...")
    try:
        pm = await get_premarket_levels(resolved)
        if pm:
            await ctx.send(embed=premarket_embed(pm))
        else:
            await ctx.send("No pre-market data available (market may not have had a pre-market session yet).")
    except Exception as exc:
        await ctx.send(f"Pre-market fetch error: {exc}")

    # Step 1: First candle
    await ctx.send("Fetching first 5-min candle...")
    try:
        fc = await get_first_candle_range(client, resolved)
        if fc:
            await ctx.send(embed=range_embed(fc))
        else:
            await ctx.send("First 5-min candle not available. Market may not be open yet or no 9:30 candle found.")
            return
    except Exception as exc:
        await ctx.send(f"First candle error: {exc}")
        return

    # Step 1.5: ATR check
    await ctx.send("Computing ATR...")
    try:
        atr = await compute_atr(client, resolved)
        if atr:
            overextended = fc.range_size > ATR_OVEREXTENDED_MULT * atr
            if overextended:
                await ctx.send(embed=overextended_embed(resolved, fc.range_size, atr))
                return
            else:
                await ctx.send(f"ATR check passed: range ${fc.range_size:.2f} <= {ATR_OVEREXTENDED_MULT}x ATR ${atr:.2f}")
        else:
            await ctx.send("Could not compute ATR (insufficient data).")
    except Exception as exc:
        await ctx.send(f"ATR error: {exc}")

    # Step 2: Daily bias
    await ctx.send("Fetching daily bias...")
    try:
        bias = await get_daily_bias(client, resolved)
        if bias:
            await ctx.send(embed=bias_embed(bias))
        else:
            await ctx.send("Could not fetch daily bias.")
            return
    except Exception as exc:
        await ctx.send(f"Bias error: {exc}")
        return

    # Step 3: Check for setup (using a temporary state so we don't affect the live one)
    await ctx.send("Checking for break-and-retest setup...")
    test_state = StrategyState()
    test_state.trading_day = datetime.now(ET).strftime("%Y-%m-%d")
    test_state.first_candle = fc
    test_state.daily_bias = bias
    test_state.premarket = pm
    if atr:
        test_state.atr_value = atr

    try:
        result = await check_for_setup(test_state, client, resolved)
        if isinstance(result, TradeSignal):
            await ctx.send("**Signal detected!**", embed=signal_embed(result))
        elif isinstance(result, FilteredSignal):
            await ctx.send("**Setup found but filtered:**", embed=filtered_embed(result))
        else:
            await ctx.send("No break-and-retest pattern detected at this time.")
    except Exception as exc:
        await ctx.send(f"Setup check error: {exc}")

    await ctx.send(f"**Test complete for {resolved}.**")


# ── Background monitor ──────────────────────────────────────

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def market_monitor():
    now = datetime.now(ET)
    time_str = now.strftime("%Y-%m-%d %I:%M:%S %p ET")

    if now.weekday() >= 5:
        logger.debug("[%s] Weekend — skipping", time_str)
        return

    today_str = now.strftime("%Y-%m-%d")
    channel = bot.get_channel(ALERT_CHANNEL_ID)

    # Reset state when the trading day changes
    first_state = states[active_tickers[0]] if active_tickers else None
    if first_state and first_state.trading_day != today_str:
        logger.info("[%s] New trading day detected (%s) — resetting state for all tickers", time_str, today_str)
        for ticker in active_tickers:
            states[ticker].reset(today_str)

        # Only send "Monitoring started" if we're within the monitoring window (market open to end)
        market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        if market_open_time <= now < monitoring_cutoff:
            for ticker in active_tickers:
                states[ticker].monitoring_started_posted = True
            if channel:
                await channel.send(
                    f"**Monitoring started for {today_str}** | "
                    f"{', '.join(active_tickers)} | {market_open_time.strftime('%I:%M %p')} - {monitoring_cutoff.strftime('%I:%M %p')} ET"
                )
            else:
                logger.error("[%s] Channel %s not found — cannot send messages", time_str, ALERT_CHANNEL_ID)
        elif now >= monitoring_cutoff:
            for ticker in active_tickers:
                states[ticker].monitoring_ended = True
            logger.info("[%s] Bot started after monitoring window — skipping session", time_str)
        else:
            logger.info("[%s] Bot started before market open — will post when market opens", time_str)

    # Trade tracking continues until market close
    tracking_cutoff = now.replace(hour=TRADE_TRACKING_END_HOUR, minute=TRADE_TRACKING_END_MINUTE, second=0, microsecond=0)
    past_tracking_cutoff = now >= tracking_cutoff

    # Stop signal monitoring after the cutoff, but track open trades
    monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
    past_monitoring = now >= monitoring_cutoff

    if past_monitoring and first_state and not first_state.monitoring_ended:
        logger.info("[%s] Monitoring window ended", time_str)
        if channel:
            for ticker in active_tickers:
                states[ticker].monitoring_ended = True
            await channel.send(embed=daily_summary_embed(today_str))

    # Expire open trades at tracking cutoff
    if past_tracking_cutoff:
        for ticker in active_tickers:
            st = states[ticker]
            if st.active_trade and not st.trade_outcome:
                st.trade_outcome = TradeOutcome(
                    ticker=ticker, direction=st.active_trade.direction,
                    entry_price=st.active_trade.entry_price,
                    exit_price=st.active_trade.entry_price,
                    stop_loss=st.active_trade.stop_loss,
                    target=st.active_trade.target,
                    result="EXPIRED", pnl=0.0,
                    timestamp=now.strftime("%I:%M %p ET"),
                )
                st.active_trade = None
                if channel:
                    await channel.send(f"**{ticker}** trade expired at market close — no target or stop hit.")
        if past_monitoring:
            return

    if channel is None:
        logger.error("[%s] Channel %s not found — skipping poll", time_str, ALERT_CHANNEL_ID)
        return

    # Track active trades (even after monitoring window closes)
    for ticker in list(active_tickers):
        st = states.get(ticker)
        if st is None:
            continue
        if st.active_trade and not st.trade_outcome:
            try:
                outcome = await check_trade_outcome(st, client, ticker)
                if outcome:
                    st.trade_outcome = outcome
                    st.active_trade = None
                    logger.info("[%s] [%s] Trade %s: %s entry=$%s exit=$%s pnl=$%s",
                                time_str, ticker, outcome.result, outcome.direction,
                                outcome.entry_price, outcome.exit_price, outcome.pnl)
                    await channel.send(embed=outcome_embed(outcome))
            except Exception as exc:
                logger.exception("[%s] [%s] Trade tracking error: %s", time_str, ticker, exc)

    if past_monitoring:
        return

    # Post "Monitoring started" at market open if bot was running before open
    if first_state and not first_state.monitoring_started_posted:
        market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        if market_open_time <= now < monitoring_cutoff:
            first_state.monitoring_started_posted = True
            if channel:
                await channel.send(
                    f"**Monitoring started for {today_str}** | "
                    f"{', '.join(active_tickers)} | {market_open_time.strftime('%I:%M %p')} - {monitoring_cutoff.strftime('%I:%M %p')} ET"
                )
                logger.info("[%s] Monitoring started message posted", time_str)

    candle_ready_time = now.replace(hour=MARKET_OPEN_HOUR, minute=FIRST_CANDLE_READY_MINUTE, second=0, microsecond=0)
    past_first_candle = now >= candle_ready_time

    if not past_first_candle:
        logger.info("[%s] Waiting for first candle (ready at %s)", time_str, candle_ready_time.strftime("%I:%M %p"))
        return

    for ticker in list(active_tickers):
        st = states.get(ticker)
        if st is None:
            continue

        if st.first_candle_overextended:
            continue
        if st.signal_fired:
            continue

        logger.info("[%s] [%s] Poll cycle | first_candle=%s | bias=%s | broke_above=%s | broke_below=%s",
                    time_str, ticker,
                    f"${st.first_candle.high}/${st.first_candle.low}" if st.first_candle else "None",
                    st.daily_bias.direction if st.daily_bias else "None",
                    st.broke_above,
                    st.broke_below)

        try:
            # Step 0: Fetch pre-market levels (once)
            if st.premarket is None:
                pm = await get_premarket_levels(ticker)
                if pm:
                    st.premarket = pm
                    logger.info("[%s] [%s] Pre-market levels: high=$%s low=$%s vol=%s",
                                time_str, ticker, pm.high, pm.low, pm.volume)
                    await channel.send(embed=premarket_embed(pm))

            # Step 1: Get first candle range
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

            # Step 1.5: Compute ATR
            if st.first_candle is not None and st.atr_value is None:
                logger.info("[%s] [%s] Computing ATR...", time_str, ticker)
                atr = await compute_atr(client, ticker)
                if atr is not None:
                    st.atr_value = atr
                    if st.first_candle.range_size > ATR_OVEREXTENDED_MULT * atr:
                        st.first_candle_overextended = True
                        logger.warning("[%s] [%s] Opening range OVEREXTENDED", time_str, ticker)
                        await channel.send(embed=overextended_embed(ticker, st.first_candle.range_size, atr))
                        continue
                    else:
                        logger.info("[%s] [%s] ATR check passed: $%.2f <= %.1fx $%.2f",
                                    time_str, ticker, st.first_candle.range_size, ATR_OVEREXTENDED_MULT, atr)

            # Step 2: Get daily bias
            if st.first_candle is not None and st.daily_bias is None:
                logger.info("[%s] [%s] Fetching daily bias...", time_str, ticker)
                bias = await get_daily_bias(client, ticker)
                if bias:
                    st.daily_bias = bias
                    logger.info("[%s] [%s] Daily bias set: %s (close=$%s sma=$%s dist=%s%%)",
                                time_str, ticker, bias.direction, bias.current_price,
                                bias.sma_value, bias.distance_pct)
                    await channel.send(embed=bias_embed(bias))
                else:
                    logger.warning("[%s] [%s] Daily bias returned None — will retry", time_str, ticker)

            # Step 3: Check for trade setups
            if st.daily_bias and st.first_candle and not st.signal_fired:
                result = await check_for_setup(st, client, ticker)
                if isinstance(result, TradeSignal):
                    filter_str = " | ".join(f"{f.name}:{f.reason}" for f in result.filters)
                    logger.info("[%s] [%s] SIGNAL FIRED: %s entry=$%s stop=$%s target=$%s | %s",
                                time_str, ticker, result.direction, result.entry_price,
                                result.stop_loss, result.target, filter_str)
                    await channel.send("@everyone", embed=signal_embed(result))
                elif isinstance(result, FilteredSignal):
                    failed = ", ".join(f"{f.name}: {f.reason}" for f in result.failed_filters)
                    logger.info("[%s] [%s] Setup FILTERED: %s | Failed: %s | Cooldown %dm",
                                time_str, ticker, result.direction, failed, FILTER_COOLDOWN_MINUTES)
                    await channel.send(embed=filtered_embed(result))
                else:
                    logger.debug("[%s] [%s] No setup detected", time_str, ticker)

        except Exception as exc:
            logger.exception("[%s] [%s] Monitor error: %s", time_str, ticker, exc)


@market_monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()


async def run_bot():
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
