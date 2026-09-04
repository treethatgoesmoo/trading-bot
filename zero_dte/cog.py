"""ZeroDTECog — the intraday forecast feature.

Owns the per-ticker StrategyState machine, the 60-second market_monitor loop
(15-min directional forecasts -> Bollinger/volume watchers -> EOD forecast
evaluation), and the 0DTE commands.

Morning bias, first-candle range/ATR-overextension, break-and-retest entry
signals + trade tracking, the 5-min companion forecast stream, the standalone
VIX spike/regime watcher, and the standalone candlestick pattern watcher were
all removed (owner decision — the live channel had too many disconnected
alert streams). See docs/state-of-the-code.md for what changed and why, and
docs/strategy-log.md for the graded-history rationale behind each removal.
The 15m forecast ensemble, the Bollinger band touch watcher, and a new
volume-spike watcher are what remains.

Cross-feature housekeeping (econ briefing, git auto-commit) lives in
bot.shared_cog so it runs even when this feature is disabled.
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import discord
from discord.ext import commands, tasks
import pytz

from config import (
    ALERT_CHANNEL_ID,
    TICKERS,
    POLL_INTERVAL_SECONDS,
    FORECAST_INTERVAL_MINUTES,
    FORECAST_POST_MODE,
    FORECAST_START_HOUR,
    FORECAST_START_MINUTE,
    FORECAST_END_HOUR,
    FORECAST_END_MINUTE,
    WATCH_INTERVAL_SECONDS,
    BB_WATCH_ENABLED,
    BB_TICKERS,
    VOLUME_WATCH_ENABLED,
    VOLUME_WATCH_TICKERS,
)
from core import market_calendar
from core.git_sync import get_version
from core.market_data import MarketDataClient, is_market_open
from core.paths import DATA_DIR, WATCHLIST_FILE
from core.tracking import append_jsonl, available_dates, load_jsonl
from zero_dte.strategy import (
    DirectionForecast,
    forecast_post_reason,
    compute_30m_forecast,
    StrategyState,
)
from zero_dte.tracker import (
    log_forecast,
    evaluate_forecasts,
    evaluate_bb_alerts,
    evaluate_volume_alerts,
)
from zero_dte.bb_watcher import BBWatcher
from zero_dte.volume_watcher import VolumeWatcher
from zero_dte.embeds import (
    bb_alert_embed,
    volume_alert_embed,
    forecast_embed,
    momentum_label,
    extension_label,
)
from zero_dte.views import ForecastView, MenuView

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
        self._watch_last_run: datetime | None = None
        self.bb_watchers: dict[str, BBWatcher] = {t: BBWatcher(t) for t in BB_TICKERS}
        self.volume_watchers: dict[str, VolumeWatcher] = {t: VolumeWatcher(t) for t in VOLUME_WATCH_TICKERS}
        self._menu_view_registered = False
        self._catchup_done = False
        self._aux_grade_day: str | None = None   # watcher-alert grading done-flag


    # ── Lifecycle ────────────────────────────────────────────────────────

    async def cog_load(self):
        self.market_monitor.start()
        logger.info("Market monitor loop started (every %ds)", POLL_INTERVAL_SECONDS)

    async def cog_unload(self):
        self.market_monitor.cancel()

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

    async def run_bb_watch(self, channel) -> None:
        """Poll Bollinger band touches on 5m and post any pings."""
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

    async def run_volume_watch(self, channel) -> None:
        """Poll relative-volume spikes on 5m and post any pings."""
        if not VOLUME_WATCH_ENABLED:
            return
        for ticker, watcher in self.volume_watchers.items():
            try:
                alerts = await watcher.check()
            except Exception as exc:
                logger.warning("[%s] Volume watch error: %s", ticker, exc)
                continue
            for alert in alerts:
                logger.info("[%s] Volume alert: %s close=%s ratio=%sx | %s",
                            ticker, alert.direction, alert.price, alert.ratio, alert.message)
                try:
                    append_jsonl("volume_alerts", datetime.now(ET).strftime("%Y-%m-%d"), {
                        "ticker": ticker, "time": datetime.now(pytz.UTC).isoformat(),
                        "timeframe": alert.timeframe, "direction": alert.direction,
                        "price": alert.price, "ratio": alert.ratio, "volume": alert.volume,
                    })
                except Exception:
                    pass
                if channel:
                    await channel.send(embed=volume_alert_embed(alert))

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
            if st is not None:
                st.last_forecast_time = now
                st.last_forecast = fc
                # On-demand posts always go out — but they still count as the
                # last posted read so the change-driven loop stays quiet until
                # something actually moves.
                st.last_posted_forecast = fc
                st.last_posted_time = now
                self._track_session_range(st, fc)
            await dest.send(embed=forecast_embed(fc), view=ForecastView(ticker, fc, entry["time"]))
        else:
            await dest.send(f"Could not compute forecast for {ticker} (insufficient data).")

    async def _post_forecast(self, channel, ticker: str, st, fc, now, entry) -> bool:
        """Post a computed (already-logged) forecast, unless change-driven mode
        decides nothing changed. Returns True if a message was sent."""
        reason = None
        if FORECAST_POST_MODE == "change":
            reason = forecast_post_reason(fc, st.last_posted_forecast, st.last_posted_time, now)
            if reason is None:
                logger.debug("[%s] %dm forecast unchanged (%s %.0f%%) — not posted",
                             ticker, fc.interval, fc.direction, fc.confidence)
                return False
        st.last_posted_forecast = fc
        st.last_posted_time = now
        content = None
        if reason and reason not in ("first read", "periodic update"):
            content = f"\U0001f514 {reason}"
        await channel.send(content, embed=forecast_embed(fc),
                           view=ForecastView(ticker, fc, entry["time"]))
        return True

    @staticmethod
    def _track_session_range(st, fc) -> None:
        """Fold a forecast's observed session extremes into the tracked ones."""
        if fc.day_high is not None:
            st.session_high = round(max(st.session_high or fc.day_high, fc.day_high), 2)
        if fc.day_low is not None:
            st.session_low = round(min(st.session_low or fc.day_low, fc.day_low), 2)

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
            status_lines = []
            if st.last_forecast:
                fc = st.last_forecast
                dir_txt = f"VOLATILE, leaning {fc.lean}" if fc.direction == "VOLATILE" and fc.lean else fc.direction
                # No confidence % here (2026-08-15: stated confidence proved
                # uncalibrated — 70-100% stated realized ~53%, see
                # docs/strategy-log.md) — momentum/extension are descriptive
                # only, shown as state not as a probability claim.
                extra = []
                if fc.momentum_score is not None:
                    extra.append(f"momentum {momentum_label(fc.momentum_score).lower()} ({fc.momentum_score:+.0f})")
                if fc.extension_score is not None:
                    extra.append(f"stretch {extension_label(fc.extension_score).lower()} ({fc.extension_score:.0f})")
                extra_txt = f" [{', '.join(extra)}]" if extra else ""
                status_lines.append(f"Intraday now: {dir_txt} ({fc.timestamp}){extra_txt}")
            else:
                status_lines.append("Intraday now: Not set")
            if st.session_high is not None and st.session_low is not None:
                status_lines.append(f"Day H/L: ${st.session_high} / ${st.session_low}")

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

    # ── Commands ─────────────────────────────────────────────────────────

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
        self._save_watchlist()
        await ctx.send(f"❌ Removed **{upper}** from watchlist. Now tracking: {', '.join(self.active_tickers)}")

    @commands.command(name="watchlist")
    async def cmd_watchlist(self, ctx):
        """Show current watchlist."""
        await ctx.send(f"**Watchlist:** {', '.join(self.active_tickers)}")

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
                    results = await evaluate_forecasts(self.client, ticker, d)
                    if results:
                        logger.info("Catch-up: graded %d forecast(s) for %s on %s", len(results), ticker, d)
                except Exception as exc:
                    logger.warning("Catch-up grading failed for %s on %s: %s", ticker, d, exc)

        # Watcher alerts from missed days grade too (idempotent — rows that
        # already carry a `correct` key are skipped).
        for kind, fn in (("bb_alerts", evaluate_bb_alerts), ("volume_alerts", evaluate_volume_alerts)):
            for d in [d for d in available_dates(kind) if d < today_str][-3:]:
                try:
                    await fn(self.client, d)
                except Exception as exc:
                    logger.warning("Catch-up %s grading failed for %s: %s", kind, d, exc)

    def _rehydrate_state(self, today_str: str) -> None:
        """After a mid-day restart, restore per-ticker forecast state from
        today's data files so already-posted forecasts don't duplicate and an
        already-graded report card doesn't re-post."""
        try:
            # Tickers whose forecast report card already graded/posted today —
            # re-running the EOD evaluation would re-post the whole embed.
            results_tickers = set()
            results_path = os.path.join(DATA_DIR, f"results-{today_str}.json")
            if os.path.exists(results_path):
                with open(results_path) as f:
                    results_tickers = {r.get("ticker") for r in json.load(f)}
            last_fc: dict[str, dict] = {}
            for e in load_jsonl("forecasts", today_str):
                if e.get("ticker") and e.get("time") and e.get("interval", FORECAST_INTERVAL_MINUTES) == FORECAST_INTERVAL_MINUTES:
                    last_fc[e["ticker"]] = e
        except Exception as exc:
            logger.warning("State rehydration failed (starting fresh): %s", exc)
            return

        for ticker in self.active_tickers:
            st = self.states[ticker]
            if ticker in results_tickers:
                st.forecast_eval_done = True  # report card already posted today
            if ticker in last_fc:
                e = last_fc[ticker]
                try:
                    fc_time = datetime.fromisoformat(e["time"]).astimezone(ET)
                except ValueError:
                    fc_time = None
                if fc_time is not None:
                    st.last_forecast_time = fc_time
                    # Minimal restore so !status can still show the latest
                    # intraday read after a mid-day restart.
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
                        interval=e.get("interval", FORECAST_INTERVAL_MINUTES),
                        day_high=e.get("day_high"),
                        day_low=e.get("day_low"),
                        atr=e.get("atr"),
                        momentum_score=e.get("momentum_score"),
                        extension_score=e.get("extension_score"),
                        candle_30m_direction=e.get("candle_30m_direction"),
                        candle_1h_direction=e.get("candle_1h_direction"),
                    )
        if last_fc:
            logger.info("Rehydrated today's forecast state from data/ (%s)", sorted(last_fc))

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
            for watcher in self.bb_watchers.values():
                watcher.reset()
            for watcher in self.volume_watchers.values():
                watcher.reset()
            self._rehydrate_state(today_str)

        session_close = market_calendar.session_close(now)
        past_eval_cutoff = now >= session_close

        if past_eval_cutoff:
            for ticker in self.active_tickers:
                st = self.states[ticker]
                if not st.forecast_eval_done:
                    evaluation_succeeded = True
                    try:
                        # Grading still runs and writes data/results-*.json
                        # every night (feeds tune_weights.py + !stats/dashboard)
                        # — only the live "Forecast Report Card" post is gone
                        # (2026-08-15: bare accuracy % with no baseline shown
                        # read as a claim it couldn't back up; see
                        # zero_dte/embeds.py docstring + docs/strategy-log.md).
                        results = await evaluate_forecasts(self.client, ticker, today_str)
                        if results:
                            logger.info("[%s] [%s] Forecast evaluation: %d forecasts scored, saved to data/results-%s.json",
                                        time_str, ticker, len(results), today_str)
                    except Exception as exc:
                        evaluation_succeeded = False
                        logger.exception("[%s] [%s] Forecast evaluation error: %s", time_str, ticker, exc)
                    if evaluation_succeeded:
                        st.forecast_eval_done = True

            # Watcher-alert grading — every ping stream must grade (house
            # rule: an ungraded alert can never earn or lose its place).
            if self._aux_grade_day != today_str:
                aux_grading_succeeded = True
                try:
                    n = await evaluate_bb_alerts(self.client, today_str)
                    if n:
                        logger.info("[%s] Graded %d BB touch alert(s)", time_str, n)
                except Exception as exc:
                    aux_grading_succeeded = False
                    logger.warning("[%s] BB alert grading error: %s", time_str, exc)
                try:
                    n = await evaluate_volume_alerts(self.client, today_str)
                    if n:
                        logger.info("[%s] Graded %d volume alert(s)", time_str, n)
                except Exception as exc:
                    aux_grading_succeeded = False
                    logger.warning("[%s] Volume alert grading error: %s", time_str, exc)
                if aux_grading_succeeded:
                    self._aux_grade_day = today_str

            return  # session's over — nothing else to do today

        if channel is None:
            logger.error("[%s] Channel %s not found — skipping poll", time_str, ALERT_CHANNEL_ID)
            return

        # 15-min directional forecasts (run throughout the trading day)
        forecast_start = now.replace(hour=FORECAST_START_HOUR, minute=FORECAST_START_MINUTE, second=0, microsecond=0)
        forecast_end = min(
            now.replace(hour=FORECAST_END_HOUR, minute=FORECAST_END_MINUTE, second=0, microsecond=0),
            session_close,
        )

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
                        self._track_session_range(st, fc)
                        entry = log_forecast(ticker, fc, now)
                        logger.info("[%s] [%s] %dm Forecast: %s (%.1f%%) | RSI=%s EMA=%s MACD=%s Div=%s",
                                    time_str, ticker, FORECAST_INTERVAL_MINUTES, fc.direction, fc.confidence,
                                    fc.rsi_30m, fc.ema_direction, fc.macd_direction, fc.divergence)
                        if channel:
                            await self._post_forecast(channel, ticker, st, fc, now, entry)
                except Exception as exc:
                    logger.exception("[%s] [%s] Forecast error: %s", time_str, ticker, exc)

        # BB + volume watchers — run all session, throttled, ping on detection
        if (is_market_open() and
                (self._watch_last_run is None or (now - self._watch_last_run).total_seconds() >= WATCH_INTERVAL_SECONDS)):
            self._watch_last_run = now
            await self.run_bb_watch(channel)
            await self.run_volume_watch(channel)

    @market_monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @market_monitor.error
    async def monitor_error(self, exc):
        """An unhandled exception kills a tasks.loop permanently — log and restart it."""
        logger.exception("Market monitor loop crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.market_monitor.restart()
