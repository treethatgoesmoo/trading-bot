"""SharedCog — cross-feature commands and daily housekeeping.

Owns the economic-event briefing (posted once per trading day at market open)
and the git auto-commit of data/ (once per day, shortly after session close).
These live here — not in a feature cog — so disabling a feature never silently
stops history archiving or the morning briefing.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
import pytz

from config import (
    ALERT_CHANNEL_ID,
    AUTO_TUNE_ENABLED,
    ECON_EVENTS_ENABLED,
    ECON_LOOKAHEAD_DAYS,
    GIT_AUTOCOMMIT_ENABLED,
    GIT_AUTOCOMMIT_PUSH,
    GIT_AUTOCOMMIT_DELAY_MINUTES,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    STARTUP_CHANGELOG_ENABLED,
)
from core import analytics, market_calendar
from core.economic_events import EconEvent, fetch_events
from core.git_sync import commit_data_history, get_latest_change, get_version
from core.market_data import MarketDataClient
from core.paths import DASHBOARD_OUT

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


def economic_events_embed(events: list[EconEvent]) -> discord.Embed:
    """Morning briefing of upcoming market-moving macro events (Kalshi-implied odds)."""
    has_today = any(e.is_today for e in events)
    embed = discord.Embed(
        title="\U0001f4c5 Economic Calendar — Major Movers",
        color=0xE74C3C if has_today else 0x5865F2,
    )

    if not events:
        embed.description = f"No major economic releases in the next {ECON_LOOKAHEAD_DAYS} days."
        embed.set_footer(text="Source: Kalshi economic markets")
        return embed

    if has_today:
        embed.description = "⚠️ A major economic release is scheduled **today** — expect volatility."

    for e in events:
        date_str = e.release_dt.strftime("%a %b %d · %I:%M %p ET")
        today_tag = "  \U0001f534 **TODAY**" if e.is_today else ""
        lines = [f"\U0001f5d3️ {date_str}{today_tag}", f"\U0001f4ca {e.consensus}"]
        # For Fed-style mutually-exclusive events, show the runner-up for context.
        if e.mutually_exclusive and len(e.top_outcomes) > 1:
            runner = e.top_outcomes[1]
            lines.append(f"↳ then {runner.label} ({runner.prob:.0%})")
        embed.add_field(name=f"{e.emoji} {e.name}", value="\n".join(lines), inline=False)

    embed.set_footer(text="Kalshi-implied odds · kalshi.com — not financial advice")
    return embed


def stats_embed(report: dict) -> discord.Embed:
    """Compact in-chat performance summary built from analytics.build_report()."""
    fc, bias, trades = report["forecast"], report["bias"], report["trades"]
    lo, hi = report["date_range"]
    n_days = len(report["dates"])

    if not n_days:
        embed = discord.Embed(
            title="\U0001f4ca Performance Stats",
            description="No history yet — run the bot through a trading day first.",
            color=0x95A5A6,
        )
        return embed

    acc = fc["accuracy"]
    color = 0x00CC66 if acc >= 55 else (0xFFAA00 if acc >= 45 else 0xFF4444)
    embed = discord.Embed(
        title="\U0001f4ca Performance Stats",
        description=f"{lo} → {hi} · {n_days} day(s) · {fc['total']} forecasts",
        color=color,
    )
    embed.add_field(
        name="Forecast Accuracy",
        value=f"**{acc:.0f}%** ({fc['correct']}/{fc['scored']}) · avg conf {fc['avg_confidence']:.0f}%",
        inline=False,
    )
    if bias["scored"]:
        embed.add_field(name="Bias Accuracy",
                        value=f"{bias['accuracy']:.0f}% ({bias['correct']}/{bias['scored']} days)", inline=True)
    if trades["wins"] + trades["losses"]:
        embed.add_field(name="Trade Win Rate",
                        value=f"{trades['win_rate']:.0f}% ({trades['wins']}W/{trades['losses']}L) · {trades['total_pnl']:+.2f} · {trades['avg_r']:+.2f}R",
                        inline=True)

    inds = report["indicators"]
    if inds:
        best = inds[:3]
        worst = [r for r in reversed(inds) if r["accuracy"] < 50][:3]
        best_str = "\n".join(f"\U0001f7e2 {r['indicator']}: {r['accuracy']:.0f}% (n={r['n']})" for r in best)
        embed.add_field(name="Best Indicators", value=best_str or "—", inline=False)
        if worst:
            worst_str = "\n".join(f"\U0001f534 {r['indicator']}: {r['accuracy']:.0f}% (n={r['n']})" for r in worst)
            embed.add_field(name="Underperforming (<50%)", value=worst_str, inline=False)

    flt = report["filters"]
    if flt["setups"]:
        embed.add_field(name="Setups Blocked by Filters",
                        value=f"{flt['blocked']}/{flt['setups']} ({flt['block_rate']:.0f}%)", inline=False)

    sw = report.get("swing") or {}
    if sw.get("signals") or sw.get("graded"):
        if sw["graded"]:
            opt = f" · opt P&L {sw['avg_option_pnl']:+.1f}%" if sw.get("avg_option_pnl") is not None else ""
            val = (f"{sw['win_rate']:.0f}% win ({sw['wins']}/{sw['graded']} graded) · "
                   f"avg move {sw['avg_move']:+.2f}%{opt} · {sw['signals']} signal(s)")
        else:
            val = f"{sw['signals']} signal(s) fired · none graded yet"
        embed.add_field(name="Swing Options", value=val, inline=False)

    embed.set_footer(text="!dashboard for the full HTML report")
    return embed


class SharedCog(commands.Cog, name="Shared"):

    def __init__(self, bot: commands.Bot, client: MarketDataClient):
        self.bot = bot
        self.client = client
        self._econ_briefing_date: str | None = None
        self._history_commit_date: str | None = None
        self._tune_date: str | None = None
        self._startup_notice_sent = False

    async def cog_load(self):
        self.housekeeping.start()
        logger.info("Housekeeping loop started (econ briefing + nightly tune + git auto-commit)")

    async def cog_unload(self):
        self.housekeeping.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Post the latest change once per process — so a restart (deploy)
        shows what's now running. on_ready re-fires on reconnects; the flag
        keeps this to the first."""
        if self._startup_notice_sent or not STARTUP_CHANGELOG_ENABLED:
            return
        self._startup_notice_sent = True
        channel = self.bot.get_channel(ALERT_CHANNEL_ID)
        if channel is None:
            return
        try:
            change = await asyncio.to_thread(get_latest_change)
            embed = discord.Embed(title="\U0001f504 Bot restarted", color=0x5865F2)
            embed.add_field(name="Version", value=get_version(), inline=True)
            if change:
                embed.add_field(name="Latest change", value=change["subject"][:1024], inline=False)
                embed.add_field(name="Committed", value=f"{change['date']} · `{change['sha']}`", inline=True)
            embed.set_footer(text="!status for live state · !stats for performance")
            await channel.send(embed=embed)
        except Exception as exc:
            logger.warning("Startup notice failed: %s", exc)

    # ── Daily jobs ───────────────────────────────────────────────────────

    async def maybe_post_econ_briefing(self, channel, today_str: str) -> None:
        """Post the economic-event briefing once per trading day. Best-effort."""
        if not ECON_EVENTS_ENABLED or channel is None:
            return
        if self._econ_briefing_date == today_str:
            return
        self._econ_briefing_date = today_str  # set first so a failure doesn't retry every poll
        try:
            events = await fetch_events()
            logger.info("Economic briefing: %d upcoming event(s) within %d days",
                        len(events), ECON_LOOKAHEAD_DAYS)
            await channel.send(embed=economic_events_embed(events))
        except Exception as exc:
            logger.warning("Economic briefing failed: %s", exc)

    async def maybe_tune_weights(self, channel, today_str: str) -> None:
        """Nightly self-tune: refresh both weight files from graded history
        (once per trading day, after close — before the git auto-commit so the
        tuned weights land in the same history commit)."""
        if not AUTO_TUNE_ENABLED or self._tune_date == today_str:
            return
        self._tune_date = today_str  # set first so a failure doesn't retry every poll

        def _tune():
            import tune_weights
            import tune_swing_weights
            return (tune_weights.retune(write=True),
                    tune_swing_weights.retune(write=True))

        try:
            fc_payload, swing_payload = await asyncio.to_thread(_tune)
        except Exception as exc:
            logger.warning("Nightly weight tuning failed: %s", exc)
            return
        parts = []
        if fc_payload:
            parts.append(f"forecast weights ({sum(fc_payload['samples'].values())} graded votes over {fc_payload['days']} day(s))")
        if swing_payload:
            parts.append(f"swing weights ({sum(swing_payload['samples'].values())} graded votes)")
        logger.info("Nightly retune: %s", " · ".join(parts) if parts else "no graded history yet")
        if parts and channel:
            await channel.send("\U0001f39b️ Nightly retune — " + " · ".join(parts) + ". Hot-reloaded live.")

    async def maybe_commit_history(self, channel, today_str: str) -> None:
        """Commit the day's captured history to git once per trading day (after close)."""
        if not GIT_AUTOCOMMIT_ENABLED or self._history_commit_date == today_str:
            return
        self._history_commit_date = today_str  # set first so a failure doesn't retry every poll
        message = f"Capture trading history for {today_str}"
        changed, detail = await asyncio.to_thread(commit_data_history, message, GIT_AUTOCOMMIT_PUSH)
        logger.info("History auto-commit (%s): %s", today_str, detail)
        if changed and channel:
            await channel.send(f"\U0001f4be History saved to git — {detail} ({today_str}).")

    @tasks.loop(seconds=60)
    async def housekeeping(self):
        now = datetime.now(ET)
        if not market_calendar.is_trading_day(now.date()):
            return
        today_str = now.strftime("%Y-%m-%d")
        channel = self.bot.get_channel(ALERT_CHANNEL_ID)

        market_open = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        if now >= market_open:
            # Only within the first 90 minutes: a bot restarted mid-afternoon
            # (or at night) shouldn't post a stale "morning" briefing.
            if now < market_open + timedelta(minutes=90):
                await self.maybe_post_econ_briefing(channel, today_str)
            elif self._econ_briefing_date != today_str:
                self._econ_briefing_date = today_str  # window missed — skip silently

        # After close: retune weights from the freshly graded day, then commit
        # (a few minutes after close so the features' EOD evaluations land first).
        commit_time = market_calendar.session_close(now) + timedelta(minutes=GIT_AUTOCOMMIT_DELAY_MINUTES)
        if now >= commit_time:
            await self.maybe_tune_weights(channel, today_str)
            await self.maybe_commit_history(channel, today_str)

    @housekeeping.before_loop
    async def before_housekeeping(self):
        await self.bot.wait_until_ready()

    @housekeeping.error
    async def housekeeping_error(self, exc):
        """An unhandled exception kills a tasks.loop permanently — log and restart it."""
        logger.exception("Housekeeping loop crashed: %s — restarting in 30s", exc)
        await asyncio.sleep(30)
        self.housekeeping.restart()

    # ── Commands ─────────────────────────────────────────────────────────

    @commands.command(name="cmd", aliases=["cmds"])
    async def cmd_cmd(self, ctx):
        """List all available commands grouped by feature."""
        embed = discord.Embed(title="\U0001f4d6 Available Commands", color=0x5865F2)
        # Built from the loaded cogs, so the list always matches the feature
        # toggles and new commands show up via their docstrings.
        for cog_name, cog in self.bot.cogs.items():
            visible = [c for c in cog.get_commands() if not c.hidden]
            if not visible:
                continue
            lines = [f"`{ctx.prefix}{c.name}` — {c.short_doc or 'No description.'}"
                     for c in sorted(visible, key=lambda c: c.name)]
            embed.add_field(name=cog_name, value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="[arg] optional · <arg> required")
        await ctx.send(embed=embed)

    @commands.command(name="events")
    async def cmd_events(self, ctx, days: int = None):
        """Show upcoming major economic events (Kalshi). Usage: !events [days]"""
        await ctx.send("Fetching economic calendar from Kalshi...")
        try:
            events = await fetch_events(lookahead_days=days)
        except Exception as exc:
            await ctx.send(f"Error fetching economic events: {exc}")
            return
        await ctx.send(embed=economic_events_embed(events))

    @commands.command(name="stats")
    async def cmd_stats(self, ctx, days: int = None):
        """Show performance stats across history. Usage: !stats [days]"""
        try:
            report = await asyncio.to_thread(analytics.build_report, days)
        except Exception as exc:
            await ctx.send(f"Error building stats: {exc}")
            return
        await ctx.send(embed=stats_embed(report))

    @commands.command(name="dashboard")
    async def cmd_dashboard(self, ctx, days: int = None):
        """Generate the full HTML performance dashboard. Usage: !dashboard [days]"""
        await ctx.send("Generating dashboard...")
        import dashboard as dashboard_gen

        def _generate() -> tuple[str, dict]:
            report = analytics.build_report(days=days)
            with open(DASHBOARD_OUT, "w", encoding="utf-8") as f:
                f.write(dashboard_gen.render(report))
            return DASHBOARD_OUT, report

        try:
            path, report = await asyncio.to_thread(_generate)
        except Exception as exc:
            await ctx.send(f"Error generating dashboard: {exc}")
            return

        fc = report["forecast"]
        msg = (f"\U0001f4c8 Dashboard written to `{path}`\n"
               f"{len(report['dates'])} day(s) · {fc['scored']} scored forecasts · {fc['accuracy']:.0f}% accuracy")
        try:
            await ctx.send(msg, file=discord.File(path))
        except Exception:
            await ctx.send(msg)

    @commands.command(name="commit")
    async def cmd_commit(self, ctx):
        """Manually commit & push the captured history (data/) to git now."""
        await ctx.send("Committing captured history to git...")
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        changed, detail = await asyncio.to_thread(
            commit_data_history, f"Manual history capture for {today_str}", GIT_AUTOCOMMIT_PUSH)
        icon = "\U0001f4be" if changed else "ℹ️"
        await ctx.send(f"{icon} {detail}.")
