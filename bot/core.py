"""Discord bot shell — builds the bot, wires in feature cogs by toggle, runs it.

Features are cogs constructed with the shared MarketDataClient injected; cogs
never import this module (dependency direction: bot → features → core).
"""

import logging

import discord
from discord.ext import commands

from config import (
    ALERT_CHANNEL_ID,
    COMMAND_PREFIX,
    DISCORD_TOKEN,
    POLL_INTERVAL_SECONDS,
    SPX_FLOW_ENABLED,
    SWING_ENABLED,
    THETADATA_API_KEY,
    ZERO_DTE_ENABLED,
)
from core.git_sync import get_version
from core.market_data import MarketDataClient

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
    client = MarketDataClient()

    @bot.event
    async def on_ready():
        logger.info("Bot connected as %s (ID: %s) | Version: %s", bot.user, bot.user.id, get_version())
        logger.info("Config: channel=%s poll=%ds | features: zero_dte=%s swing=%s spx_flow=%s",
                    ALERT_CHANNEL_ID, POLL_INTERVAL_SECONDS, ZERO_DTE_ENABLED,
                    SWING_ENABLED, SPX_FLOW_ENABLED)
        logger.info("Data sources: TwelveData=%s Finnhub=%s yfinance=always",
                    "configured" if client.twelvedata_limiter else "not set",
                    "configured" if client.finnhub_limiter else "not set")

    async with bot:
        from bot.shared_cog import SharedCog
        await bot.add_cog(SharedCog(bot, client))
        if ZERO_DTE_ENABLED:
            from zero_dte.cog import ZeroDTECog
            await bot.add_cog(ZeroDTECog(bot, client))
        if SWING_ENABLED:
            from swing.cog import SwingCog
            await bot.add_cog(SwingCog(bot, client))
        if SPX_FLOW_ENABLED:
            if not THETADATA_API_KEY:
                logger.error("SPX flow watch enabled but THETADATA_API_KEY is missing; feature not started")
            else:
                from core.thetadata_client import ThetaDataClient
                from zero_dte.flow_cog import SPXFlowCog
                await bot.add_cog(SPXFlowCog(bot, client, ThetaDataClient()))
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await client.close()
