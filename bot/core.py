"""Discord shell for the single Bollinger-event notification stream.

Historical feature modules remain available to offline research, but cannot be
registered through old environment flags or a notification-mode selector.
"""

import logging

import discord
from discord.ext import commands

from config import (
    COMMAND_PREFIX,
    DISCORD_TOKEN,
    SPX_CONTEXT_CHANNEL_ID,
    THETADATA_API_KEY,
)
from core.git_sync import get_version

logger = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    return commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


async def run_bot():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env file")
        print("1. Create a bot at https://discord.com/developers/applications")
        print("2. Copy the token into your .env file")
        return

    bot = build_bot()

    @bot.event
    async def on_ready():
        logger.info("Bot connected as %s (ID: %s) | Version: %s", bot.user, bot.user.id, get_version())
        logger.info("BB events: 1m/5m/15m; private channel=%s", SPX_CONTEXT_CHANNEL_ID)

    async with bot:
        from bot.shared_cog import SharedCog
        await bot.add_cog(SharedCog(bot))
        if not THETADATA_API_KEY:
            logger.error("BB events require THETADATA_API_KEY; no market notification stream started")
        else:
            from core.thetadata_client import ThetaDataClient
            from zero_dte.bb_signal_cog import BBSignalCog
            await bot.add_cog(BBSignalCog(bot, ThetaDataClient(purpose="bb_events")))
            logger.info("BB event stream installed; old SPX actions and legacy notification systems removed")
            if not SPX_CONTEXT_CHANNEL_ID:
                logger.warning("SPX_CONTEXT_CHANNEL_ID is unset: collecting locally, no notifications")
        await bot.start(DISCORD_TOKEN)
