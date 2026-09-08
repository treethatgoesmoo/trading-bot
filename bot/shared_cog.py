"""Operational commands and quiet history housekeeping.

No market messages, startup posts, automatic retuning, or historical trading
commands live here. Licensed Theta evidence is not part of data/ auto-commit.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from config import (
    GIT_AUTOCOMMIT_ENABLED,
    GIT_AUTOCOMMIT_PUSH,
    GIT_AUTOCOMMIT_DELAY_MINUTES,
    SPX_CONTEXT_CHANNEL_ID,
)
from core import market_calendar
from core.git_sync import commit_data_history

logger = logging.getLogger(__name__)


class SharedCog(commands.Cog, name="Shared"):
    def __init__(self, bot: commands.Bot, client=None):
        # Optional client keeps direct historical callers compatible; the
        # active assembly no longer constructs an unused MarketDataClient.
        self.bot = bot
        self._history_commit_date: str | None = None

    async def cog_load(self):
        self.housekeeping.start()
        logger.info("Quiet history housekeeping started")

    async def cog_unload(self):
        self.housekeeping.cancel()

    async def maybe_commit_history(self, today_str: str) -> None:
        """Archive tracked data/ once after close, without Discord publication."""
        if not GIT_AUTOCOMMIT_ENABLED or self._history_commit_date == today_str:
            return
        self._history_commit_date = today_str
        _, detail = await asyncio.to_thread(
            commit_data_history, f"Capture trading history for {today_str}", GIT_AUTOCOMMIT_PUSH)
        logger.info("History auto-commit (%s): %s", today_str, detail)

    @tasks.loop(seconds=60)
    async def housekeeping(self):
        now = datetime.now(market_calendar.ET)
        if not market_calendar.is_trading_day(now.date()):
            return
        commit_time = market_calendar.session_close(now) + timedelta(minutes=GIT_AUTOCOMMIT_DELAY_MINUTES)
        if now >= commit_time:
            await self.maybe_commit_history(now.strftime("%Y-%m-%d"))

    @housekeeping.before_loop
    async def before_housekeeping(self):
        await self.bot.wait_until_ready()

    @housekeeping.error
    async def housekeeping_error(self, _exc):
        logger.warning("History housekeeping interrupted; restarting in 30 seconds")
        await asyncio.sleep(30)
        self.housekeeping.restart()

    @commands.command(name="cmd", aliases=["cmds"])
    async def cmd_cmd(self, ctx):
        """List the commands currently installed."""
        embed = discord.Embed(title="Available Commands", color=0x5865F2)
        for cog_name, cog in self.bot.cogs.items():
            visible = [command for command in cog.get_commands() if not command.hidden]
            if not visible:
                continue
            lines = [f"`{ctx.prefix}{command.name}` — {command.short_doc or 'No description.'}"
                     for command in sorted(visible, key=lambda command: command.name)]
            embed.add_field(name=cog_name, value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="BB observations are available in the configured private channel.")
        await ctx.send(embed=embed)

    @commands.command(name="commit")
    @commands.is_owner()
    async def cmd_commit(self, ctx):
        """Owner only: commit tracked history from the configured private channel."""
        if (not SPX_CONTEXT_CHANNEL_ID
                or getattr(getattr(ctx, 'channel', None), 'id', None) != SPX_CONTEXT_CHANNEL_ID):
            await ctx.send("History administration is available only in the configured private channel.")
            return
        await ctx.send("Committing captured history to git...")
        today_str = datetime.now(market_calendar.ET).strftime("%Y-%m-%d")
        changed, detail = await asyncio.to_thread(
            commit_data_history, f"Manual history capture for {today_str}", GIT_AUTOCOMMIT_PUSH)
        await ctx.send(f"{'History saved' if changed else 'History unchanged'} — {detail}.")
