"""ZeroDTECog — the intraday first-candle feature.

Owns the per-ticker StrategyState machine, the 60-second market_monitor loop
(premarket → first candle → ATR → bias → break-and-retest → trade tracking →
30-min forecasts → RSI/VIX watchers → EOD evaluation), and the 0DTE commands.

Cross-feature housekeeping (econ briefing, git auto-commit) lives in
bot.shared_cog so it runs even when this feature is disabled.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
import pytz

from config import (
    ALERT_CHANNEL_ID,
    DAY_FORECAST_ENABLED,
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
    FORECAST_INTERVAL_MINUTES,
    FORECAST_5M_ENABLED,
    FORECAST_5M_INTERVAL_MINUTES,
    FORECAST_POST_MODE,
    FORECAST_START_HOUR,
    FORECAST_START_MINUTE,
    FORECAST_END_HOUR,
    FORECAST_END_MINUTE,
    RSI_WATCH_ENABLED,
    RSI_WATCH_INTERVAL_SECONDS,
    SPX_OPTIONS_ENABLED,
    SPX_OPTIONS_ORB_MINUTES,
    SPX_OPTIONS_ORB_TARGET_MULT,
    SPX_OPTIONS_PICK_DELAY_MINUTES,
    SPX_OPTIONS_SAMPLE_MINUTES,
    SPX_OPTIONS_TICKER,
    VIX_WATCH_ENABLED,
    BB_WATCH_ENABLED,
    BB_TICKERS,
    OPTION_PROJECTION_ENABLED,
    OPTION_PROJECTION_TICKERS,
)
from core import market_calendar
from core import vix as vix_mod
from core.git_sync import get_version
from core.market_data import MarketDataClient, is_market_open
from core.paths import DATA_DIR, WATCHLIST_FILE
from core.tracking import append_jsonl, available_dates, load_jsonl
from zero_dte.strategy import (
    DayForecast,
    DirectionForecast,
    forecast_post_reason,
    get_daily_bias,
    set_bias_from_first_candle,
    get_first_candle_range,
    get_premarket_levels,
    check_for_setup,
    check_trade_outcome,
    compute_atr,
    compute_30m_forecast,
    compute_day_forecast,
    StrategyState,
    TradeSignal,
    FilteredSignal,
    TradeOutcome,
)
from zero_dte.tracker import (
    direction_record,
    grade_option_projections,
    log_forecast,
    log_day_forecast,
    log_option_projections,
    evaluate_forecasts,
    evaluate_votes,
    log_trade_outcome,
    log_bias,
    log_filters,
    evaluate_bias,
)
from zero_dte import spx_options
from zero_dte.rsi_watcher import RSIWatcher, TIMEFRAMES, TF_LABEL
from zero_dte.bb_watcher import BBWatcher
from zero_dte.options0dte import build_projections
from zero_dte.embeds import (
    bb_alert_embed,
    bias_embed,
    daily_summary_embed,
    day_forecast_embed,
    filtered_embed,
    forecast_embed,
    forecast_results_embed,
    option_projection_embed,
    outcome_embed,
    overextended_embed,
    premarket_embed,
    range_embed,
    rsi_alert_embed,
    signal_embed,
    spx_pick_embed,
    spx_range_embed,
    vix_alert_embed,
)
from zero_dte.views import ForecastView, MenuView, ReportDetailsView, TradeSignalView

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


class ZeroDTECog(commands.Cog, name="ZeroDTE"):

    def __init__(self, bot: commands.Bot, client: MarketDataClient):
        self.bot = bot
        self.client = client
        self.version = get_version()

        self.active_tickers: list[str] = list(TICKERS)
        self.states: dict[str, StrategyState] = {t: StrategyState() for t in self.active_tickers}
        self._load_watchlist()
        self.rsi_watchers: dict[str, RSIWatcher] = {t: RSIWatcher(t) for t in self.active_tickers}
        self._rsi_last_run: datetime | None = None
        self.vix_watcher = vix_mod.VixWatcher()
        self.bb_watchers: dict[str, BBWatcher] = {t: BBWatcher(t) for t in BB_TICKERS}
        self._menu_view_registered = False
        self._catchup_done = False

        # 0DTE SPX option range tracking (own loop, per-day done-flags)
        self.spx_day: spx_options.SpxOptionDay | None = None
        self._spx_date: str | None = None
        self._spx_last_sample: datetime | None = None
        self._spx_pick_attempts = 0

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def cog_load(self):
        self.market_monitor.start()
        logger.info("Market monitor loop started (every %ds)", POLL_INTERVAL_SECONDS)
        if SPX_OPTIONS_ENABLED:
            self.spx_options_monitor.start()
            logger.info("SPX 0DTE option range loop started (%s, sampled every %dm)",
                        SPX_OPTIONS_TICKER, SPX_OPTIONS_SAMPLE_MINUTES)

    async def cog_unload(self):
        self.market_monitor.cancel()
        self.spx_options_monitor.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._menu_view_registered:
            self.bot.add_view(MenuView(self))
            self._menu_view_registered = True

    # ── Watchlist persistence ────────────────────────────────────────────

    def _load_watchlist(self):
        """Load saved watchlist and merge with env tickers."""
        if os.path.exists(WATCHLIST_FILE):
            try:
                with open(WATCHLIST_FILE) as f:
                    saved = json.load(f)
                for t in saved:
                    if t not in self.active_tickers:
                        self.active_tickers.append(t)
                        self.states[t] = StrategyState()
            except Exception:
                pass

    def _save_watchlist(self):
        try:
            with open(WATCHLIST_FILE, "w") as f:
                json.dump(self.active_tickers, f)
        except Exception:
            pass

    def _resolve_ticker(self, arg: str | None) -> str | None:
        if arg is None:
            return self.active_tickers[0] if len(self.active_tickers) == 1 else None
        upper = arg.upper()
        return upper if upper in self.states else None

    # ── Watchers ─────────────────────────────────────────────────────────

    async def run_rsi_watch(self, channel) -> None:
        """Poll every watched ticker's RSI on 5m+15m and post any reversal pings."""
        if not RSI_WATCH_ENABLED:
            return
        for ticker in list(self.active_tickers):
            watcher = self.rsi_watchers.get(ticker)
            if watcher is None:
                watcher = self.rsi_watchers[ticker] = RSIWatcher(ticker)
            try:
                alerts = await watcher.check(self.client)
            except Exception as exc:
                logger.warning("[%s] RSI watch error: %s", ticker, exc)
                continue
            embeds = []
            confirmed_any = False
            for alert in alerts:
                logger.info("[%s] RSI alert: %s %s %s (%s) conf=%s | %s",
                            ticker, alert.timeframe, alert.kind, alert.direction,
                            alert.rsi, alert.confirmed, alert.message)
                try:
                    append_jsonl("rsi_alerts", datetime.now(ET).strftime("%Y-%m-%d"), {
                        "ticker": ticker, "time": datetime.now(pytz.UTC).isoformat(),
                        "timeframe": alert.timeframe, "kind": alert.kind,
                        "direction": alert.direction, "stage": alert.stage,
                        "tier": alert.tier, "rsi": alert.rsi, "confirmed": alert.confirmed,
                    })
                except Exception:
                    pass
                confirmed_any = confirmed_any or alert.confirmed
                embeds.append(rsi_alert_embed(alert))
            # One message per ticker per cycle — simultaneous 5m/15m signals
            # used to arrive as 2-4 separate pings.
            if channel and embeds:
                content = "\U0001f514 **Confirmed RSI reversal**" if confirmed_any else None
                for i in range(0, len(embeds), 10):
                    await channel.send(content if i == 0 else None, embeds=embeds[i:i + 10])

    async def run_bb_watch(self, channel) -> None:
        """Poll Bollinger band touches on 1m+5m and post any pings."""
        if not BB_WATCH_ENABLED:
            return
        for ticker, watcher in self.bb_watchers.items():
            try:
                alerts = await watcher.check()
            except Exception as exc:
                logger.warning("[%s] BB watch error: %s", ticker, exc)
                continue
            for alert in alerts:
                logger.info("[%s] BB alert: %s %s close=%s band=%s | %s",
                            ticker, alert.timeframe, alert.band, alert.price,
                            alert.band_value, alert.message)
                try:
                    append_jsonl("bb_alerts", datetime.now(ET).strftime("%Y-%m-%d"), {
                        "ticker": ticker, "time": datetime.now(pytz.UTC).isoformat(),
                        "timeframe": alert.timeframe, "band": alert.band,
                        "price": alert.price, "band_value": alert.band_value,
                        "middle": alert.middle,
                    })
                except Exception:
                    pass
                if channel:
                    await channel.send(embed=bb_alert_embed(alert))

    async def run_vix_watch(self, channel) -> None:
        """Poll VIX for regime crossings / spikes and post any risk pings."""
        if not VIX_WATCH_ENABLED:
            return
        try:
            alerts = await self.vix_watcher.check()
        except Exception as exc:
            logger.warning("VIX watch error: %s", exc)
            return
        for alert in alerts:
            logger.info("VIX alert: %s %s level=%s move=%s | %s",
                        alert.kind, alert.direction, alert.level, alert.change_pct, alert.message)
            try:
                append_jsonl("vix_alerts", datetime.now(ET).strftime("%Y-%m-%d"), {
                    "time": datetime.now(pytz.UTC).isoformat(), "kind": alert.kind,
                    "direction": alert.direction, "level": alert.level, "change_pct": alert.change_pct,
                })
            except Exception:
                pass
            if channel:
                await channel.send(embed=vix_alert_embed(alert))

    # ── Shared actions (commands + MenuView) ─────────────────────────────

    async def send_forecast(self, dest, ticker: str):
        """Compute, log, and post an on-demand forecast with vote buttons."""
        fc = await compute_30m_forecast(self.client, ticker)
        if fc:
            now = datetime.now(ET)
            entry = log_forecast(ticker, fc, now)
            # Counts as this window's forecast: keeps the scheduled loop from
            # posting a near-duplicate a minute later.
            st = self.states.get(ticker)
            day_fc = None
            if st is not None:
                st.last_forecast_time = now
                st.last_forecast = fc
                # On-demand posts always go out — but they still count as the
                # last posted read so the change-driven loop stays quiet until
                # something actually moves.
                st.last_posted_forecast = fc
                st.last_posted_time = now
                day_fc = st.day_forecast
            await dest.send(embed=forecast_embed(fc, day_fc), view=ForecastView(ticker, fc, entry["time"]))
        else:
            await dest.send(f"Could not compute forecast for {ticker} (insufficient data).")

    async def _post_forecast(self, channel, ticker: str, st, fc, now, entry) -> bool:
        """Post a computed (already-logged) forecast, unless change-driven mode
        decides nothing changed. Returns True if a message was sent."""
        reason = None
        if FORECAST_POST_MODE == "change":
            reason = forecast_post_reason(fc, st.last_posted_forecast, st.last_posted_time,
                                          st.day_forecast, now)
            if reason is None:
                logger.debug("[%s] %dm forecast unchanged (%s %.0f%%) — not posted",
                             ticker, fc.interval, fc.direction, fc.confidence)
                return False
        st.last_posted_forecast = fc
        st.last_posted_time = now
        content = None
        if reason and reason not in ("first read", "periodic update"):
            content = f"\U0001f514 {reason}"
        await channel.send(content, embed=forecast_embed(fc, st.day_forecast),
                           view=ForecastView(ticker, fc, entry["time"]))
        return True

    async def _recover_day_forecast(self, ticker: str, st) -> None:
        """Rebuild the morning day forecast after a restart that lost it (and
        that has nothing persisted to restore — e.g. the bot was down at bias
        time). Quiet: fills in state so intraday forecast embeds regain the
        projected range, but never posts the day-forecast embed itself."""
        st.day_forecast_attempts += 1
        try:
            if st.first_candle is None:
                st.first_candle = await get_first_candle_range(self.client, ticker)
            if st.first_candle is None:
                return
            if st.daily_bias is None:
                bias = await get_daily_bias(self.client, ticker, today_open=st.first_candle.open_price)
                if bias is None:
                    return
                set_bias_from_first_candle(bias, st.first_candle)
                st.daily_bias = bias
            day_fc = await compute_day_forecast(self.client, ticker, st.daily_bias, st.first_candle)
            if day_fc:
                st.day_forecast = day_fc
                log_day_forecast(day_fc, datetime.now(ET))
                logger.info("[%s] Day forecast recovered mid-day: %s high=$%s low=$%s",
                            ticker, day_fc.direction, day_fc.exp_high, day_fc.exp_low)
        except Exception as exc:
            logger.warning("[%s] Day forecast recovery failed (attempt %d): %s",
                           ticker, st.day_forecast_attempts, exc)

    async def _post_option_projection(self, channel, ticker: str, day_fc, now) -> None:
        """Quote the ATM 0DTE call/put and post their projected price ranges
        derived from the day forecast. Logs every projection for EOD grading.
        Never raises — a chain failure must not break the bias step."""
        if not OPTION_PROJECTION_ENABLED or ticker not in OPTION_PROJECTION_TICKERS:
            return
        try:
            projections = await build_projections(
                ticker, day_fc.open_price, day_fc.exp_high, day_fc.exp_low)
            if not projections:
                return
            log_option_projections(projections, now)
            await channel.send(embed=option_projection_embed(ticker, day_fc, projections))
            logger.info("[%s] Option projection posted: %s", ticker,
                        ", ".join(f"{p.kind} {p.strike:g} ~${p.premium:.2f} → [{p.proj_low:.2f}..{p.proj_high:.2f}]"
                                  for p in projections))
        except Exception as exc:
            logger.warning("[%s] Option projection error: %s", ticker, exc)

    def build_status_embed(self) -> discord.Embed:
        now = datetime.now(ET)
        market = "OPEN" if is_market_open() else "CLOSED"
        monitoring = "Active" if self.market_monitor.is_running() else "Idle"

        embed = discord.Embed(title="⚙️ Bot Status", color=0x95A5A6)
        embed.add_field(name="Market", value=market, inline=True)
        embed.add_field(name="Monitoring", value=monitoring, inline=True)
        embed.add_field(name="Time (ET)", value=now.strftime("%I:%M %p"), inline=True)
        embed.add_field(name="Version", value=self.version, inline=True)
        embed.add_field(name="Tickers", value=", ".join(self.active_tickers), inline=True)

        for ticker in self.active_tickers:
            st = self.states[ticker]
            bias_str = st.daily_bias.direction if st.daily_bias else "Not set"
            candle_str = f"${st.first_candle.high} / ${st.first_candle.low}" if st.first_candle else "Not set"
            atr_str = f"${st.atr_value:.2f}" if st.atr_value else "N/A"
            overext_str = " (OVEREXTENDED)" if st.first_candle_overextended else ""

            status_lines = [
                f"Morning bias: {bias_str}",
                f"Candle: {candle_str}",
                f"ATR: {atr_str}{overext_str}",
            ]
            if st.last_forecast:
                fc = st.last_forecast
                dir_txt = f"VOLATILE, leaning {fc.lean}" if fc.direction == "VOLATILE" and fc.lean else fc.direction
                status_lines.insert(1, f"Intraday now: {dir_txt} ({fc.confidence}%, {fc.timestamp})")

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
        if self.client.twelvedata_limiter:
            remaining = self.client.twelvedata_limiter.remaining_daily
            sources.append(f"TwelveData: {remaining} calls left")
        else:
            sources.append("TwelveData: not configured")
        if self.client.finnhub_limiter:
            sources.append("Finnhub: active")
        else:
            sources.append("Finnhub: not configured")
        sources.append("yfinance: fallback ready")
        embed.add_field(name="Data Sources", value="\n".join(sources), inline=False)
        return embed

    async def run_test(self, ctx, resolved: str):
        """Full strategy dry run. ctx is anything with an async send() (Context or channel)."""
        await ctx.send(f"**Running full strategy test for {resolved}...**")

        # Step 0: Pre-market levels
        await ctx.send("Fetching pre-market levels...")
        pm = None
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
            fc = await get_first_candle_range(self.client, resolved)
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
        atr = None
        try:
            atr = await compute_atr(self.client, resolved)
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

        # Step 2: Daily bias (with first candle direction + gap)
        await ctx.send("Fetching daily bias...")
        try:
            bias = await get_daily_bias(self.client, resolved, today_open=fc.open_price)
            if bias:
                set_bias_from_first_candle(bias, fc)
                await ctx.send(embed=bias_embed(bias))
            else:
                await ctx.send("Could not fetch daily bias.")
                return
        except Exception as exc:
            await ctx.send(f"Bias error: {exc}")
            return

        # Step 2.5: intraday forecast (with the day forecast for context)
        await ctx.send(f"Computing {FORECAST_INTERVAL_MINUTES}-min forecast...")
        day_fc = None
        try:
            day_fc = await compute_day_forecast(self.client, resolved, bias, fc)
        except Exception:
            pass
        try:
            forecast = await compute_30m_forecast(self.client, resolved)
            if forecast:
                await ctx.send(embed=forecast_embed(forecast, day_fc))
            else:
                await ctx.send("Could not compute forecast (insufficient data).")
        except Exception as exc:
            await ctx.send(f"Forecast error: {exc}")

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
            result = await check_for_setup(test_state, self.client, resolved)
            if isinstance(result, TradeSignal):
                await ctx.send("**Signal detected!**",
                               embed=signal_embed(result, record=direction_record(result.direction)))
            elif isinstance(result, FilteredSignal):
                await ctx.send("**Setup found but filtered:**", embed=filtered_embed(result))
            else:
                await ctx.send("No break-and-retest pattern detected at this time.")
        except Exception as exc:
            await ctx.send(f"Setup check error: {exc}")

        await ctx.send(f"**Test complete for {resolved}.**")

    # ── Commands ─────────────────────────────────────────────────────────

    @commands.command(name="bias")
    async def cmd_bias(self, ctx, ticker: str = None):
        """Get today's daily bias. Usage: !bias [ticker]"""
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            await ctx.send(f"Specify a ticker: `!bias <{'|'.join(self.active_tickers)}>`")
            return
        st = self.states[resolved]
        if st.daily_bias is not None:
            await ctx.send(embed=bias_embed(st.daily_bias))
            return
        # Display-only: the monitor owns the live bias. A command must never
        # overwrite it with a computation missing the gap/first-candle inputs.
        await ctx.send(f"Fetching daily bias for {resolved}...")
        try:
            today_open = st.first_candle.open_price if st.first_candle else None
            bias = await get_daily_bias(self.client, resolved, today_open=today_open)
            if bias is not None and st.first_candle is not None:
                set_bias_from_first_candle(bias, st.first_candle)
        except Exception as exc:
            await ctx.send(f"Error fetching data: {exc}")
            return
        if bias is None:
            await ctx.send("Could not fetch daily data. Market may be closed.")
            return
        note = None if st.first_candle else "Preliminary — first 5-min candle not set yet (final bias at 9:35 ET)."
        await ctx.send(note, embed=bias_embed(bias))

    @commands.command(name="range")
    async def cmd_range(self, ctx, ticker: str = None):
        """Get today's first 5-minute candle range. Usage: !range [ticker]"""
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            await ctx.send(f"Specify a ticker: `!range <{'|'.join(self.active_tickers)}>`")
            return
        st = self.states[resolved]
        if st.first_candle is not None:
            await ctx.send(embed=range_embed(st.first_candle))
            return
        # Display-only: before 9:35 the 9:30 bar is still forming, and storing
        # a partial box here would corrupt the live break-and-retest state.
        await ctx.send(f"Fetching first candle range for {resolved}...")
        try:
            fc = await get_first_candle_range(self.client, resolved)
        except Exception as exc:
            await ctx.send(f"Error fetching data: {exc}")
            return
        if fc is None:
            await ctx.send("First 5-min candle not available yet. Wait until after 9:35 AM ET.")
            return
        await ctx.send(embed=range_embed(fc))

    @commands.command(name="status")
    async def cmd_status(self, ctx):
        """Show current bot status for all tickers."""
        await ctx.send(embed=self.build_status_embed())

    @commands.command(name="add")
    async def cmd_add(self, ctx, ticker: str = None):
        """Add a ticker to the watchlist. Usage: !add AAPL"""
        if ticker is None:
            await ctx.send("Usage: `!add <TICKER>`")
            return
        upper = ticker.upper()
        if upper in self.states:
            await ctx.send(f"{upper} is already being tracked.")
            return
        self.active_tickers.append(upper)
        self.states[upper] = StrategyState()
        self.rsi_watchers[upper] = RSIWatcher(upper)
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        self.states[upper].trading_day = today_str
        self._save_watchlist()
        await ctx.send(f"✅ Added **{upper}** to watchlist. Now tracking: {', '.join(self.active_tickers)}")

    @commands.command(name="remove")
    async def cmd_remove(self, ctx, ticker: str = None):
        """Remove a ticker from the watchlist. Usage: !remove AAPL"""
        if ticker is None:
            await ctx.send("Usage: `!remove <TICKER>`")
            return
        upper = ticker.upper()
        if upper not in self.states:
            await ctx.send(f"{upper} is not being tracked.")
            return
        self.active_tickers.remove(upper)
        del self.states[upper]
        self.rsi_watchers.pop(upper, None)
        self._save_watchlist()
        await ctx.send(f"❌ Removed **{upper}** from watchlist. Now tracking: {', '.join(self.active_tickers)}")

    @commands.command(name="watchlist")
    async def cmd_watchlist(self, ctx):
        """Show current watchlist."""
        await ctx.send(f"**Watchlist:** {', '.join(self.active_tickers)}")

    @commands.command(name="rsi")
    async def cmd_rsi(self, ctx, ticker: str = None):
        """Show current 5m/15m RSI and run the reversal watcher. Usage: !rsi [ticker]"""
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            await ctx.send(f"Specify a ticker: `!rsi <{'|'.join(self.active_tickers)}>`")
            return
        from core.indicators import rsi_series

        lines = []
        watcher = self.rsi_watchers.get(resolved) or RSIWatcher(resolved)
        posted = 0
        for tf in TIMEFRAMES:
            try:
                df = await self.client.fetch_candles(resolved, interval=tf, count=120)
                r = rsi_series(df["close"].astype(float), watcher.period)
                val = r.iloc[-1]
                zone = ("overbought" if val >= 70 else "oversold" if val <= 30 else "neutral")
                lines.append(f"**{TF_LABEL[tf]}**: RSI {val:.1f} ({zone})")
            except Exception as exc:
                lines.append(f"**{TF_LABEL[tf]}**: error — {exc}")

        embed = discord.Embed(title=f"📈 RSI — {resolved}", color=0x3498DB,
                              description="\n".join(lines))
        embed.set_footer(text="Live watcher pings on threshold/divergence/failure-swing reversals")
        await ctx.send(embed=embed)

        # Also surface any live signal right now.
        try:
            alerts = await watcher.check(self.client)
            for alert in alerts:
                await ctx.send(embed=rsi_alert_embed(alert))
                posted += 1
        except Exception:
            pass
        if not posted:
            await ctx.send("No active RSI reversal signal at the moment.")

    @commands.command(name="vix")
    async def cmd_vix(self, ctx):
        """Show the current VIX level, daily change, and regime."""
        snap = await vix_mod.get_snapshot()
        if snap is None:
            await ctx.send("Could not fetch VIX right now.")
            return
        regime_color = {"CALM": 0x00CC66, "NORMAL": 0x00CC66, "ELEVATED": 0xFFAA00,
                        "HIGH": 0xFF6600, "FEAR": 0xFF4444}.get(snap.regime, 0x95A5A6)
        arrow = "⬆️" if snap.change > 0 else ("⬇️" if snap.change < 0 else "➡️")
        embed = discord.Embed(title=f"\U0001f4c9 VIX {snap.level:.2f} — {snap.regime}", color=regime_color)
        embed.add_field(name="Daily change", value=f"{arrow} {snap.change:+.2f} ({snap.change_pct:+.2f}%)", inline=True)
        embed.add_field(name="Intraday", value=snap.direction, inline=True)
        spy_lean = "SPY tailwind" if snap.direction == "FALLING" else ("SPY headwind" if snap.direction == "RISING" else "neutral")
        embed.add_field(name="Inverse read", value=spy_lean, inline=True)
        embed.set_footer(text="VIX is inverse to SPY · feeds the 30-min forecast + spike alerts")
        await ctx.send(embed=embed)

    @commands.command(name="test")
    async def cmd_test(self, ctx, ticker: str = None):
        """Simulate the full strategy flow for a ticker. Usage: !test [ticker]"""
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            await ctx.send(f"Specify a ticker: `!test <{'|'.join(self.active_tickers)}>`")
            return
        await self.run_test(ctx, resolved)

    @commands.command(name="spxrange")
    async def cmd_spxrange(self, ctx):
        """Show today's tracked 0DTE SPX call/put premium range so far."""
        if not SPX_OPTIONS_ENABLED:
            await ctx.send("SPX 0DTE range tracking is disabled (SPX_OPTIONS_ENABLED=0).")
            return
        day = self.spx_day
        if day is None:
            await ctx.send(f"No {SPX_OPTIONS_TICKER} 0DTE contracts tracked yet today — "
                           f"the ATM call/put are picked ~{SPX_OPTIONS_PICK_DELAY_MINUTES} min "
                           "after the open on trading days.")
            return
        await ctx.send(embed=spx_range_embed(day))

    @commands.command(name="menu")
    async def cmd_menu(self, ctx):
        """Show the interactive control panel."""
        embed = discord.Embed(
            title="🤖 Trading Bot Control Panel",
            description="Pick a ticker from the dropdown, then use the buttons.",
            color=0x5865F2,
        )
        await ctx.send(embed=embed, view=MenuView(self))

    @commands.command(name="forecast")
    async def cmd_forecast(self, ctx, ticker: str = None):
        """Get an on-demand intraday forecast. Usage: !forecast [ticker]"""
        resolved = self._resolve_ticker(ticker)
        if resolved is None:
            await ctx.send(f"Specify a ticker: `!forecast <{'|'.join(self.active_tickers)}>`")
            return
        await ctx.send(f"Computing {FORECAST_INTERVAL_MINUTES}-min forecast for {resolved}...")
        await self.send_forecast(ctx, resolved)

    # ── Background monitor ───────────────────────────────────────────────

    async def _post_monitoring_started(self, channel, now, today_str: str) -> None:
        """Announce the monitoring window (once per day)."""
        for ticker in self.active_tickers:
            self.states[ticker].monitoring_started_posted = True
        market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        await channel.send(
            f"**Monitoring started for {today_str}** | "
            f"{', '.join(self.active_tickers)} | {market_open_time.strftime('%I:%M %p')} - {monitoring_cutoff.strftime('%I:%M %p')} ET"
        )

    async def _grade_missed_days(self):
        """Catch-up after downtime: EOD evaluation only runs while the bot is
        up, so a day that ended during an outage never got graded (its
        results-*.json is missing). Grade the most recent such days now."""
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        dates = [d for d in available_dates("forecasts") if d < today_str][-3:]
        for d in dates:
            if os.path.exists(os.path.join(DATA_DIR, f"results-{d}.json")):
                continue
            tickers = {e.get("ticker") for e in load_jsonl("forecasts", d)}
            for ticker in sorted(t for t in tickers if t):
                try:
                    await evaluate_bias(self.client, ticker, d)
                    results = await evaluate_forecasts(self.client, ticker, d)
                    if results:
                        logger.info("Catch-up: graded %d forecast(s) for %s on %s", len(results), ticker, d)
                except Exception as exc:
                    logger.warning("Catch-up grading failed for %s on %s: %s", ticker, d, exc)

    def _rehydrate_state(self, today_str: str) -> None:
        """After a mid-day restart, restore per-ticker flags from today's data
        files so already-alerted signals/forecasts/bias don't fire again."""
        try:
            bias_tickers = {e.get("ticker") for e in load_jsonl("bias", today_str)}
            fired_tickers = {e.get("ticker") for e in load_jsonl("filters", today_str) if e.get("fired")}
            outcomes = {e.get("ticker"): e for e in load_jsonl("outcomes", today_str)}
            day_fcs = {e.get("ticker"): e for e in load_jsonl("day_forecast", today_str)}
            last_fc: dict[str, dict] = {}
            last_fc_5m: dict[str, dict] = {}
            for e in load_jsonl("forecasts", today_str):
                if e.get("ticker") and e.get("time"):
                    # The 5m companion stream throttles separately; only the
                    # main-interval stream is the canonical "last forecast".
                    if e.get("interval", FORECAST_INTERVAL_MINUTES) == FORECAST_INTERVAL_MINUTES:
                        last_fc[e["ticker"]] = e
                    else:
                        last_fc_5m[e["ticker"]] = e
        except Exception as exc:
            logger.warning("State rehydration failed (starting fresh): %s", exc)
            return

        any_activity = bool(bias_tickers or last_fc or last_fc_5m or fired_tickers)
        for ticker in self.active_tickers:
            st = self.states[ticker]
            if any_activity:
                st.monitoring_started_posted = True  # already announced before the restart
            if ticker in bias_tickers:
                st.bias_logged = True  # don't re-log/re-post bias or day forecast
            if ticker in last_fc:
                e = last_fc[ticker]
                try:
                    fc_time = datetime.fromisoformat(e["time"]).astimezone(ET)
                except ValueError:
                    fc_time = None
                if fc_time is not None:
                    st.last_forecast_time = fc_time
                    # Minimal restore so the daily summary / !status can still
                    # show the latest intraday read after a mid-day restart.
                    st.last_forecast = DirectionForecast(
                        ticker=ticker,
                        direction=e.get("direction", "NEUTRAL"),
                        confidence=e.get("confidence", 0.0),
                        timestamp=fc_time.strftime("%I:%M %p ET"),
                        current_price=e.get("price", 0.0),
                        vwap=e.get("vwap"),
                        rsi_30m=e.get("rsi_30m"),
                        ema_direction=e.get("ema_direction"),
                        macd_direction=e.get("macd_direction"),
                        divergence=e.get("divergence"),
                        signals=e.get("signals", {}),
                        lean=e.get("lean"),
                    )
            if ticker in last_fc_5m:
                try:
                    st.last_forecast_5m_time = datetime.fromisoformat(last_fc_5m[ticker]["time"]).astimezone(ET)
                except ValueError:
                    pass
            if ticker in day_fcs:
                e = day_fcs[ticker]
                try:
                    st.day_forecast = DayForecast(
                        ticker=ticker, direction=e["direction"], strength=e["strength"],
                        open_price=e["open_price"], exp_high=e["exp_high"], exp_low=e["exp_low"],
                        day_atr=e["day_atr"], pivot=e["pivot"], r1=e["r1"], s1=e["s1"],
                        prev_high=e["prev_high"], prev_low=e["prev_low"],
                        timestamp=e.get("timestamp", ""),
                    )
                except KeyError as exc:
                    logger.warning("[%s] Day forecast restore missing field %s", ticker, exc)
            if ticker in fired_tickers:
                st.signal_fired = True
                out = outcomes.get(ticker)
                if out:
                    st.trade_outcome = TradeOutcome(
                        ticker=ticker, direction=out.get("direction", "?"),
                        entry_price=out.get("entry_price", 0.0), exit_price=out.get("exit_price", 0.0),
                        stop_loss=out.get("stop_loss", 0.0), target=out.get("target", 0.0),
                        result=out.get("result", "?"), pnl=out.get("pnl", 0.0),
                        timestamp="restored after restart",
                    )
                else:
                    logger.warning("[%s] Signal fired before restart but no outcome recorded — "
                                   "its trade can't be tracked further", ticker)
        if any_activity:
            logger.info("Rehydrated today's state from data/ (bias=%s fired=%s forecasts=%s)",
                        sorted(t for t in bias_tickers if t), sorted(t for t in fired_tickers if t),
                        sorted(last_fc))

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def market_monitor(self):
        now = datetime.now(ET)
        time_str = now.strftime("%Y-%m-%d %I:%M:%S %p ET")

        if not self._catchup_done:
            self._catchup_done = True
            try:
                await self._grade_missed_days()
            except Exception as exc:
                logger.warning("Catch-up grading pass failed: %s", exc)

        if not market_calendar.is_trading_day(now.date()):
            logger.debug("[%s] Weekend/market holiday — skipping", time_str)
            return

        today_str = now.strftime("%Y-%m-%d")
        channel = self.bot.get_channel(ALERT_CHANNEL_ID)

        # Reset state when the trading day changes
        first_state = self.states[self.active_tickers[0]] if self.active_tickers else None
        if first_state and first_state.trading_day != today_str:
            logger.info("[%s] New trading day detected (%s) — resetting state for all tickers", time_str, today_str)
            for ticker in self.active_tickers:
                self.states[ticker].reset(today_str)
                self.rsi_watchers.setdefault(ticker, RSIWatcher(ticker)).reset()
            self.vix_watcher.reset()
            for watcher in self.bb_watchers.values():
                watcher.reset()
            self._rehydrate_state(today_str)

            # Only send "Monitoring started" if we're within the monitoring window (market open to end)
            market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
            monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
            if first_state.monitoring_started_posted:
                pass  # rehydrated — announced before the restart
            elif market_open_time <= now < monitoring_cutoff:
                if channel:
                    await self._post_monitoring_started(channel, now, today_str)
                else:
                    for ticker in self.active_tickers:
                        self.states[ticker].monitoring_started_posted = True
                    logger.error("[%s] Channel %s not found — cannot send messages", time_str, ALERT_CHANNEL_ID)
            elif now >= monitoring_cutoff:
                for ticker in self.active_tickers:
                    self.states[ticker].monitoring_ended = True
                logger.info("[%s] Bot started after monitoring window — skipping session", time_str)
            else:
                logger.info("[%s] Bot started before market open — will post when market opens", time_str)

        # Trade tracking continues until session close (13:00 ET on half-days)
        session_close = market_calendar.session_close(now)
        tracking_cutoff = min(
            now.replace(hour=TRADE_TRACKING_END_HOUR, minute=TRADE_TRACKING_END_MINUTE, second=0, microsecond=0),
            session_close,
        )
        past_tracking_cutoff = now >= tracking_cutoff

        # Stop signal monitoring after the cutoff, but track open trades
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        past_monitoring = now >= monitoring_cutoff

        if past_monitoring and first_state and not first_state.monitoring_ended:
            logger.info("[%s] Monitoring window ended", time_str)
            # Flag first: a missing channel must not leave this re-running (and
            # re-posting a stale summary) every poll for the rest of the day.
            for ticker in self.active_tickers:
                self.states[ticker].monitoring_ended = True
            if channel:
                await channel.send(embed=daily_summary_embed(today_str, self.active_tickers, self.states))

        # Expire open trades at tracking cutoff
        if past_tracking_cutoff:
            for ticker in self.active_tickers:
                st = self.states[ticker]
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
                    log_trade_outcome(st.trade_outcome, now)
                    if channel:
                        await channel.send(f"**{ticker}** trade expired at market close — no target or stop hit.")

                # Evaluate today's forecasts + bias against actual outcomes (once per day)
                if not st.forecast_eval_done:
                    st.forecast_eval_done = True
                    try:
                        await evaluate_bias(self.client, ticker, today_str)
                    except Exception as exc:
                        logger.warning("[%s] [%s] Bias evaluation error: %s", time_str, ticker, exc)
                    try:
                        graded = await grade_option_projections(ticker, today_str)
                        if graded:
                            logger.info("[%s] [%s] Option projections graded: %d contract(s) → data/option_outcomes-%s.jsonl",
                                        time_str, ticker, len(graded), today_str)
                    except Exception as exc:
                        logger.warning("[%s] [%s] Option projection grading error: %s", time_str, ticker, exc)
                    try:
                        results = await evaluate_forecasts(self.client, ticker, today_str)
                        if results:
                            user_score = evaluate_votes(ticker, today_str, results)
                            logger.info("[%s] [%s] Forecast evaluation: %d forecasts scored, saved to data/results-%s.json",
                                        time_str, ticker, len(results), today_str)
                            if channel:
                                await channel.send(
                                    embed=forecast_results_embed(ticker, today_str, results, user_score),
                                    view=ReportDetailsView(ticker, today_str, results),
                                )
                    except Exception as exc:
                        logger.exception("[%s] [%s] Forecast evaluation error: %s", time_str, ticker, exc)

            if past_monitoring:
                return

        if channel is None:
            logger.error("[%s] Channel %s not found — skipping poll", time_str, ALERT_CHANNEL_ID)
            return

        # Track active trades (even after monitoring window closes)
        for ticker in list(self.active_tickers):
            st = self.states.get(ticker)
            if st is None:
                continue
            if st.active_trade and not st.trade_outcome:
                try:
                    outcome = await check_trade_outcome(st, self.client, ticker)
                    if outcome:
                        st.trade_outcome = outcome
                        st.active_trade = None
                        logger.info("[%s] [%s] Trade %s: %s entry=$%s exit=$%s pnl=$%s",
                                    time_str, ticker, outcome.result, outcome.direction,
                                    outcome.entry_price, outcome.exit_price, outcome.pnl)
                        log_trade_outcome(outcome, now)
                        await channel.send(embed=outcome_embed(outcome))
                except Exception as exc:
                    logger.exception("[%s] [%s] Trade tracking error: %s", time_str, ticker, exc)

        # 30-min directional forecasts (run throughout the trading day)
        forecast_start = now.replace(hour=FORECAST_START_HOUR, minute=FORECAST_START_MINUTE, second=0, microsecond=0)
        forecast_end = min(
            now.replace(hour=FORECAST_END_HOUR, minute=FORECAST_END_MINUTE, second=0, microsecond=0),
            session_close,
        )

        # Day-forecast recovery: a restart with nothing persisted to rehydrate
        # (or one after the 11:00 monitoring window, where the bias step no
        # longer runs) leaves forecasts without the projected range — rebuild
        # it here. Pre-11:00 first starts are left to the normal bias step so
        # the bias/day-forecast embeds still post. Throttled to forecast slots
        # and capped so a bad data day can't hammer the API every 60s.
        if (DAY_FORECAST_ENABLED and forecast_start <= now < forecast_end
                and now.minute % FORECAST_5M_INTERVAL_MINUTES < 2):
            for ticker in list(self.active_tickers):
                st = self.states.get(ticker)
                if (st is None or st.day_forecast is not None or st.day_forecast_attempts >= 10
                        or not (st.bias_logged or past_monitoring)):
                    continue
                await self._recover_day_forecast(ticker, st)

        if forecast_start <= now < forecast_end and now.minute % FORECAST_INTERVAL_MINUTES < 2:
            for ticker in list(self.active_tickers):
                st = self.states.get(ticker)
                if st is None:
                    continue
                if st.last_forecast_time and (now - st.last_forecast_time).total_seconds() < (FORECAST_INTERVAL_MINUTES * 60 - 30):
                    continue
                try:
                    fc = await compute_30m_forecast(self.client, ticker)
                    if fc:
                        st.last_forecast_time = now
                        st.last_forecast = fc
                        entry = log_forecast(ticker, fc, now)
                        logger.info("[%s] [%s] %dm Forecast: %s (%.1f%%) | RSI=%s EMA=%s MACD=%s Div=%s",
                                    time_str, ticker, FORECAST_INTERVAL_MINUTES, fc.direction, fc.confidence,
                                    fc.rsi_30m, fc.ema_direction, fc.macd_direction, fc.divergence)
                        if channel:
                            await self._post_forecast(channel, ticker, st, fc, now, entry)
                except Exception as exc:
                    logger.exception("[%s] [%s] Forecast error: %s", time_str, ticker, exc)

        # Fast companion forecast on 5-min candles, same window
        if (FORECAST_5M_ENABLED and forecast_start <= now < forecast_end
                and now.minute % FORECAST_5M_INTERVAL_MINUTES < 2):
            for ticker in list(self.active_tickers):
                st = self.states.get(ticker)
                if st is None:
                    continue
                if st.last_forecast_5m_time and (now - st.last_forecast_5m_time).total_seconds() < (FORECAST_5M_INTERVAL_MINUTES * 60 - 30):
                    continue
                try:
                    fc5 = await compute_30m_forecast(self.client, ticker, interval=FORECAST_5M_INTERVAL_MINUTES)
                    if fc5:
                        st.last_forecast_5m_time = now
                        entry = log_forecast(ticker, fc5, now)
                        logger.info("[%s] [%s] %dm Forecast: %s (%.1f%%) | RSI=%s EMA=%s MACD=%s Div=%s",
                                    time_str, ticker, FORECAST_5M_INTERVAL_MINUTES, fc5.direction, fc5.confidence,
                                    fc5.rsi_30m, fc5.ema_direction, fc5.macd_direction, fc5.divergence)
                        if channel:
                            await self._post_forecast(channel, ticker, st, fc5, now, entry)
                except Exception as exc:
                    logger.exception("[%s] [%s] 5m forecast error: %s", time_str, ticker, exc)

        # RSI + VIX watchers — run all session, throttled, ping on detection
        if (is_market_open() and
                (self._rsi_last_run is None or (now - self._rsi_last_run).total_seconds() >= RSI_WATCH_INTERVAL_SECONDS)):
            self._rsi_last_run = now
            if RSI_WATCH_ENABLED:
                await self.run_rsi_watch(channel)
            await self.run_vix_watch(channel)
            await self.run_bb_watch(channel)

        if past_monitoring:
            return

        # Post "Monitoring started" at market open if bot was running before open
        if first_state and not first_state.monitoring_started_posted:
            market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
            monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
            if market_open_time <= now < monitoring_cutoff:
                await self._post_monitoring_started(channel, now, today_str)
                logger.info("[%s] Monitoring started message posted", time_str)

        candle_ready_time = now.replace(hour=MARKET_OPEN_HOUR, minute=FIRST_CANDLE_READY_MINUTE, second=0, microsecond=0)
        past_first_candle = now >= candle_ready_time

        if not past_first_candle:
            logger.info("[%s] Waiting for first candle (ready at %s)", time_str, candle_ready_time.strftime("%I:%M %p"))
            return

        for ticker in list(self.active_tickers):
            st = self.states.get(ticker)
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
                    fc = await get_first_candle_range(self.client, ticker)
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
                    atr = await compute_atr(self.client, ticker)
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

                # Step 2: Get daily bias + set direction from first candle
                if st.first_candle is not None and st.daily_bias is None:
                    logger.info("[%s] [%s] Fetching daily bias...", time_str, ticker)
                    bias = await get_daily_bias(self.client, ticker, today_open=st.first_candle.open_price)
                    if bias:
                        set_bias_from_first_candle(bias, st.first_candle)
                        st.daily_bias = bias
                        logger.info("[%s] [%s] Daily bias set: %s (gap=%s %+.2f%% | fc=%s | rsi=%s)",
                                    time_str, ticker, bias.direction, bias.gap_direction,
                                    bias.gap_pct, st.first_candle.candle_type,
                                    bias.rsi if bias.rsi else "N/A")
                        first_post = not st.bias_logged
                        if first_post:
                            st.bias_logged = True
                            log_bias(bias, now)
                            await channel.send(embed=bias_embed(bias))
                        # Compute even when rehydrated after a restart (the
                        # in-memory day forecast is gone but intraday forecast
                        # embeds still want it) — only post it the first time.
                        if DAY_FORECAST_ENABLED and st.day_forecast is None:
                            try:
                                day_fc = await compute_day_forecast(self.client, ticker, bias, st.first_candle)
                                if day_fc:
                                    st.day_forecast = day_fc
                                    log_day_forecast(day_fc, now)
                                    if first_post:
                                        await channel.send(embed=day_forecast_embed(day_fc))
                                        await self._post_option_projection(channel, ticker, day_fc, now)
                            except Exception as exc:
                                logger.warning("[%s] [%s] Day forecast error: %s", time_str, ticker, exc)
                    else:
                        logger.warning("[%s] [%s] Daily bias returned None — will retry", time_str, ticker)

                # Step 3: Check for trade setups
                if st.daily_bias and st.first_candle and not st.signal_fired:
                    result = await check_for_setup(st, self.client, ticker)
                    if isinstance(result, TradeSignal):
                        filter_str = " | ".join(f"{f.name}:{f.reason}" for f in result.filters)
                        logger.info("[%s] [%s] SIGNAL FIRED: %s entry=$%s stop=$%s target=$%s | %s",
                                    time_str, ticker, result.direction, result.entry_price,
                                    result.stop_loss, result.target, filter_str)
                        log_filters(ticker, result.direction, result.filters, fired=True, now=now)
                        await channel.send("@everyone",
                                           embed=signal_embed(result, record=direction_record(result.direction)),
                                           view=TradeSignalView(result, self.states))
                    elif isinstance(result, FilteredSignal):
                        failed = ", ".join(f"{f.name}: {f.reason}" for f in result.failed_filters)
                        logger.info("[%s] [%s] Setup FILTERED: %s | Failed: %s | Cooldown %dm",
                                    time_str, ticker, result.direction, failed, FILTER_COOLDOWN_MINUTES)
                        log_filters(ticker, result.direction, result.filters, fired=False, now=now)
                        await channel.send(embed=filtered_embed(result))
                    else:
                        logger.debug("[%s] [%s] No setup detected", time_str, ticker)

            except Exception as exc:
                logger.exception("[%s] [%s] Monitor error: %s", time_str, ticker, exc)

    @market_monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @market_monitor.error
    async def monitor_error(self, exc):
        """An unhandled exception kills a tasks.loop permanently — log and restart it."""
        logger.exception("Market monitor loop crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.market_monitor.restart()

    # ── SPX 0DTE option range tracking ───────────────────────────────────

    async def _project_spx_ranges(self, day, now, today_str: str) -> bool:
        """Seed the projected premium ranges from the ORB opening range (first
        SPX_OPTIONS_ORB_MINUTES of the session). The last 5-min bar of the
        window can lag a poll or two — callers retry until this returns True
        (or the day ends without one)."""
        orb = await asyncio.to_thread(
            spx_options.fetch_opening_range, day.ticker, now.date(), SPX_OPTIONS_ORB_MINUTES)
        if orb is None:
            return False
        spx_options.apply_orb_projection(day, orb[0], orb[1], SPX_OPTIONS_ORB_TARGET_MULT)
        if day.proj_high is None:
            return False
        append_jsonl("spx_options", today_str, spx_options.projection_entry(day))
        logger.info("[SPX 0DTE] Opening range $%.2f-$%.2f -> breakout targets $%.2f / $%.2f | "
                    "call could reach ~$%.2f, put ~$%.2f",
                    day.or_low, day.or_high, day.proj_low, day.proj_high,
                    day.call.projected_max, day.put.projected_max)
        return True

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def spx_options_monitor(self):
        """Pick an ATM 0DTE call + put shortly after the open, sample their
        premiums through the session, and post the effective min/max at the
        close (min floored to settlement intrinsic — OTM finishes are worth 0)."""
        now = datetime.now(ET)
        if not market_calendar.is_trading_day(now.date()):
            return
        today_str = now.strftime("%Y-%m-%d")

        if self._spx_date != today_str:
            self._spx_date = today_str
            self.spx_day = None
            self._spx_last_sample = None
            self._spx_pick_attempts = 0
            try:
                self.spx_day = spx_options.rehydrate(load_jsonl("spx_options", today_str))
                if self.spx_day is not None:
                    logger.info("[SPX 0DTE] Rehydrated %s tracking from data/ (finalized=%s)",
                                self.spx_day.ticker, self.spx_day.finalized)
            except Exception as exc:
                logger.warning("[SPX 0DTE] Rehydration failed (starting fresh): %s", exc)

        day = self.spx_day
        if day is not None and day.finalized:
            return

        session_close = market_calendar.session_close(now)
        pick_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
                                second=0, microsecond=0) + timedelta(minutes=SPX_OPTIONS_PICK_DELAY_MINUTES)
        channel = self.bot.get_channel(ALERT_CHANNEL_ID)

        # Step 1: pick the day's contracts (retry a few polls, then give up
        # for the day — no chain before ~9:35 is normal, 10 misses is not).
        if day is None:
            if now < pick_time or now >= session_close or self._spx_pick_attempts >= 10:
                return
            self._spx_pick_attempts += 1
            day = await asyncio.to_thread(
                spx_options.pick_day_contracts, SPX_OPTIONS_TICKER, now.date())
            if day is None:
                return
            self.spx_day = day
            self._spx_last_sample = now
            append_jsonl("spx_options", today_str, spx_options.pick_entry(day))
            await self._project_spx_ranges(day, now, today_str)
            logger.info("[SPX 0DTE] Tracking %s: call %s @ $%.2f | put %s @ $%.2f (spot $%.2f)",
                        day.date, day.call.strike, day.call.open_mid,
                        day.put.strike, day.put.open_mid, day.spot_at_pick)
            if channel:
                await channel.send(embed=spx_pick_embed(day))
            return

        # Step 2: sample premiums until the close.
        if now < session_close:
            if (self._spx_last_sample is not None and
                    (now - self._spx_last_sample).total_seconds() < SPX_OPTIONS_SAMPLE_MINUTES * 60 - 30):
                return
            self._spx_last_sample = now
            if day.proj_high is None:
                await self._project_spx_ranges(day, now, today_str)
            if await asyncio.to_thread(spx_options.sample_day, day):
                append_jsonl("spx_options", today_str, spx_options.sample_entry(day))
            return

        # Step 3: past the close — settle and post the effective range once.
        spot_close = await asyncio.to_thread(spx_options.fetch_close_spot, day.ticker)
        await asyncio.to_thread(spx_options.finalize_day, day, spot_close)
        append_jsonl("spx_options", today_str, spx_options.final_entry(day))
        logger.info("[SPX 0DTE] Final: call %s eff $%.2f-$%.2f | put %s eff $%.2f-$%.2f (close $%s)",
                    day.call.strike, day.call.effective_min, day.call.effective_max,
                    day.put.strike, day.put.effective_min, day.put.effective_max,
                    day.spot_close)
        if channel:
            await channel.send(embed=spx_range_embed(day))

    @spx_options_monitor.before_loop
    async def before_spx_options(self):
        await self.bot.wait_until_ready()

    @spx_options_monitor.error
    async def spx_options_error(self, exc):
        """An unhandled exception kills a tasks.loop permanently — log and restart it."""
        logger.exception("SPX 0DTE range loop crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.spx_options_monitor.restart()
