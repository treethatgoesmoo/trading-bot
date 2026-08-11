"""SwingCog — daily-timeframe option-buy signals for popular names.

Scheduling: a 60-second loop with per-day done-flags (never `tasks.loop(time=)`
which is UTC/holiday-blind, and never minute-modulo windows which double-fire).

  Pre-open pass (~30 min before open): grade due signals, then scan each
  watchlist ticker and post new signals with a suggested 7-14 DTE contract.
  Midday pass (12:30 ET): check open signals for target/invalidation.

The pre-open pass also self-heals: signals that matured while the bot was
offline get graded on the next pass because grading iterates the open-signal
registry (data/swing_open.json).
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
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    SWING_ALERT_CHANNEL_ID,
    SWING_COOLDOWN_DAYS,
    SWING_MIDDAY_HOUR,
    SWING_MIDDAY_MINUTE,
    SWING_PREOPEN_OFFSET_MINUTES,
    SWING_TICKERS,
)
from core import market_calendar
from core.market_data import MarketDataClient
from core.paths import SWING_STATE_FILE, SWING_WATCHLIST_FILE
from core.tracking import write_atomic
from swing.embeds import (
    swing_analysis_embed,
    swing_check_embed,
    swing_grade_embed,
    swing_signal_embed,
)
from swing.options import pick_contract
from swing.strategy import check_price_vs_signal, evaluate_daily, scan_ticker
from swing.tracker import (
    grade_due_signals,
    last_signal_dates,
    load_open_signals,
    log_swing_check,
    log_swing_signal,
    set_signal_status,
)

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


class SwingCog(commands.Cog, name="Swing"):

    def __init__(self, bot: commands.Bot, client: MarketDataClient):
        self.bot = bot
        self.client = client
        self.watchlist: list[str] = list(SWING_TICKERS)
        self._load_watchlist()
        self._preopen_done_date: str | None = None
        self._midday_done_date: str | None = None
        self._load_daily_state()

    async def cog_load(self):
        self.swing_monitor.start()
        logger.info("Swing monitor loop started (tickers: %s)", ", ".join(self.watchlist))

    async def cog_unload(self):
        self.swing_monitor.cancel()

    # ── Watchlist persistence ────────────────────────────────────────────

    def _load_watchlist(self):
        if os.path.exists(SWING_WATCHLIST_FILE):
            try:
                with open(SWING_WATCHLIST_FILE) as f:
                    saved = json.load(f)
                for t in saved:
                    if t not in self.watchlist:
                        self.watchlist.append(t)
            except Exception:
                pass

    def _save_watchlist(self):
        try:
            write_atomic(SWING_WATCHLIST_FILE, json.dumps(self.watchlist))
        except Exception as exc:
            logger.warning("Swing watchlist save failed: %s", exc)

    # ── Done-flag persistence ────────────────────────────────────────────
    # The per-day done-flags must survive restarts: in-process-only flags
    # replayed the full pre-open pass after any mid-session restart, posting
    # a fresh @everyone signal priced off a session-stale close.

    def _load_daily_state(self):
        try:
            with open(SWING_STATE_FILE) as f:
                saved = json.load(f)
            self._preopen_done_date = saved.get("preopen")
            self._midday_done_date = saved.get("midday")
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Swing daily-state load failed (flags reset): %s", exc)

    def _save_daily_state(self):
        try:
            write_atomic(SWING_STATE_FILE, json.dumps(
                {"preopen": self._preopen_done_date, "midday": self._midday_done_date}))
        except Exception as exc:
            logger.warning("Swing daily-state save failed: %s", exc)

    # ── Scheduled passes ─────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def swing_monitor(self):
        now = datetime.now(ET)
        if not market_calendar.is_trading_day(now.date()):
            return
        today = now.strftime("%Y-%m-%d")

        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE,
                                  second=0, microsecond=0)
        preopen_t = market_open - timedelta(minutes=SWING_PREOPEN_OFFSET_MINUTES)
        midday_t = now.replace(hour=SWING_MIDDAY_HOUR, minute=SWING_MIDDAY_MINUTE,
                               second=0, microsecond=0)

        if now >= preopen_t and self._preopen_done_date != today:
            self._preopen_done_date = today  # set first — a crash must not retry every minute
            self._save_daily_state()
            await self._preopen_pass(now)

        if now >= midday_t and self._midday_done_date != today:
            self._midday_done_date = today
            self._save_daily_state()
            await self._midday_pass(now)

    @swing_monitor.before_loop
    async def before_swing_monitor(self):
        await self.bot.wait_until_ready()

    @swing_monitor.error
    async def swing_monitor_error(self, exc):
        """An unhandled exception kills a tasks.loop permanently — log and restart it."""
        logger.exception("Swing monitor loop crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.swing_monitor.restart()

    def _channel(self):
        return self.bot.get_channel(SWING_ALERT_CHANNEL_ID)

    async def _preopen_pass(self, now: datetime) -> None:
        """Grade matured signals, then scan the watchlist for new ones."""
        channel = self._channel()
        logger.info("Swing pre-open pass (%s)", now.strftime("%Y-%m-%d %I:%M %p ET"))

        # 1. Grade anything due (also catches signals that matured while offline).
        try:
            outcomes = await grade_due_signals(self.client, now)
            for outcome in outcomes:
                if channel:
                    await channel.send(embed=swing_grade_embed(outcome))
        except Exception as exc:
            logger.exception("Swing grading error: %s", exc)

        # 2. Scan for new signals — only while the morning context holds. After
        # a long outage this pass can first run mid-afternoon: the scan would
        # sign off yesterday's close as the entry price (a full session stale)
        # and the midday confirmation for it has already passed. Grading above
        # still ran (that's the self-heal); new signals wait for tomorrow.
        midday_t = now.replace(hour=SWING_MIDDAY_HOUR, minute=SWING_MIDDAY_MINUTE,
                               second=0, microsecond=0)
        if now >= midday_t:
            logger.info("Swing scan skipped — pre-open pass first ran at %s (past midday)",
                        now.strftime("%I:%M %p ET"))
            return

        open_tickers = {s["ticker"] for s in load_open_signals()}
        recent = last_signal_dates(SWING_COOLDOWN_DAYS + 30)
        today = now.date()
        for ticker in list(self.watchlist):
            if ticker in open_tickers:
                logger.info("[%s] Swing scan skipped — signal already open", ticker)
                continue
            try:
                sig = await scan_ticker(self.client, ticker)
                if sig is None:
                    continue
                last = recent.get((ticker, sig.direction))
                if last:
                    last_d = datetime.strptime(last, "%Y-%m-%d").date()
                    if (today - last_d).days < SWING_COOLDOWN_DAYS:
                        logger.info("[%s] Swing %s suppressed — cooldown (last %s)",
                                    ticker, sig.direction, last)
                        continue
                try:
                    sig.contract = await asyncio.to_thread(
                        pick_contract, ticker, sig.direction, sig.vote.close)
                except Exception as exc:
                    logger.warning("[%s] Contract pick failed: %s", ticker, exc)
                    sig.contract = None
                log_swing_signal(sig)
                logger.info("[%s] SWING SIGNAL: %s conf=%.1f%% score=%+.1f contract=%s",
                            ticker, sig.direction, sig.vote.confidence, sig.vote.score,
                            sig.contract.contract_symbol if sig.contract else "none")
                if channel:
                    await channel.send("@everyone", embed=swing_signal_embed(sig))
            except Exception as exc:
                logger.exception("[%s] Swing scan error: %s", ticker, exc)

    async def _midday_pass(self, now: datetime) -> None:
        """Check open signals for intraday target/invalidation."""
        channel = self._channel()
        open_sigs = load_open_signals()
        if not open_sigs:
            return
        logger.info("Swing midday pass — %d open signal(s)", len(open_sigs))
        for sig in open_sigs:
            if sig.get("status") in ("TARGET", "INVALIDATED"):
                continue  # already flagged; grading finalizes at next pre-open
            try:
                df = await self.client.fetch_candles(sig["ticker"], interval="5", count=5)
                if df is None or df.empty:
                    continue
                last_price = round(float(df["close"].astype(float).iloc[-1]), 2)
                status = check_price_vs_signal(sig, last_price)
                log_swing_check({
                    "signal_id": sig["signal_id"], "ticker": sig["ticker"],
                    "time": now.astimezone(pytz.UTC).isoformat(), "price": last_price,
                    "status": status or "OK",
                }, now)
                if status:
                    set_signal_status(sig["signal_id"], status)
                    logger.info("[%s] Swing midday: %s at $%s", sig["ticker"], status, last_price)
                    if channel:
                        await channel.send(embed=swing_check_embed(sig, last_price, status))
            except Exception as exc:
                logger.exception("[%s] Swing midday check error: %s", sig["ticker"], exc)

    # ── Commands ─────────────────────────────────────────────────────────

    @commands.command(name="swing")
    async def cmd_swing(self, ctx, ticker: str = None):
        """On-demand swing analysis for any ticker (not tracked). Usage: !swing TSLA"""
        if ticker is None:
            await ctx.send(f"Usage: `!swing <TICKER>` — watchlist: {', '.join(self.watchlist)}")
            return
        upper = ticker.upper()
        await ctx.send(f"Analyzing {upper} on the daily timeframe...")
        try:
            df = await self.client.fetch_candles(upper, interval="D", count=300)
        except Exception as exc:
            await ctx.send(f"Error fetching daily data: {exc}")
            return
        vote = evaluate_daily(df)
        if vote is None:
            await ctx.send(f"Not enough daily history for {upper} (need ~210 bars).")
            return
        contract = None
        if vote.direction in ("BULLISH", "BEARISH"):
            direction = "CALL" if vote.direction == "BULLISH" else "PUT"
            try:
                contract = await asyncio.to_thread(pick_contract, upper, direction, vote.close)
            except Exception:
                contract = None
        await ctx.send(embed=swing_analysis_embed(upper, vote, contract))

    @commands.command(name="swingopen")
    async def cmd_swingopen(self, ctx):
        """List currently open (ungraded) swing signals."""
        open_sigs = load_open_signals()
        if not open_sigs:
            await ctx.send("No open swing signals.")
            return
        lines = []
        for s in open_sigs:
            c = s.get("contract") or {}
            lines.append(
                f"**{s['ticker']} {s['direction']}** — {s['signal_date']} @ ${s['entry_price']} "
                f"(inval ${s['invalidation']}, status {s.get('status', 'OPEN')}"
                + (f", {c.get('contract_symbol')}" if c else "") + ")"
            )
        await ctx.send(embed=discord.Embed(
            title=f"\U0001f4c1 Open Swing Signals ({len(open_sigs)})",
            description="\n".join(lines), color=0x3498DB))

    @commands.command(name="swinglist")
    async def cmd_swinglist(self, ctx):
        """Show the swing watchlist."""
        await ctx.send(f"**Swing watchlist:** {', '.join(self.watchlist)}")

    @commands.command(name="swingadd")
    async def cmd_swingadd(self, ctx, ticker: str = None):
        """Add a ticker to the swing watchlist. Usage: !swingadd NVDA"""
        if ticker is None:
            await ctx.send("Usage: `!swingadd <TICKER>`")
            return
        upper = ticker.upper()
        if upper in self.watchlist:
            await ctx.send(f"{upper} is already on the swing watchlist.")
            return
        self.watchlist.append(upper)
        self._save_watchlist()
        await ctx.send(f"✅ Added **{upper}** to swing watchlist: {', '.join(self.watchlist)}")

    @commands.command(name="swingremove")
    async def cmd_swingremove(self, ctx, ticker: str = None):
        """Remove a ticker from the swing watchlist. Usage: !swingremove NVDA"""
        if ticker is None:
            await ctx.send("Usage: `!swingremove <TICKER>`")
            return
        upper = ticker.upper()
        if upper not in self.watchlist:
            await ctx.send(f"{upper} is not on the swing watchlist.")
            return
        self.watchlist.remove(upper)
        self._save_watchlist()
        await ctx.send(f"❌ Removed **{upper}** from swing watchlist: {', '.join(self.watchlist)}")
