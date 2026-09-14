"""Discord assembly: SPY setup signals for manually executed SPX options."""
import logging

import discord
from discord.ext import commands

import config
from spy_setup.cog import SpySetupCog
from spy_setup.signals import VERSION

logger = logging.getLogger(__name__)


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    return commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=intents)


async def install_service(bot):
    """Install cached status even when requests are disabled or unconfigured."""
    client = None
    if config.COLLECTION_ENABLED and config.THETADATA_API_KEY:
        from core.thetadata_client import ThetaDataClient
        client = ThetaDataClient(purpose='spy_setup')
    await bot.add_cog(SpySetupCog(bot, client))
    collecting = client is not None
    publishing = collecting and config.PUBLICATION_ENABLED and bool(config.SPX_CHANNEL_ID)
    logger.info('SPX %s | collection=%s | publication=%s | private channel=%s',
                VERSION, collecting, publishing, config.SPX_CHANNEL_ID)
    if config.COLLECTION_ENABLED and not config.THETADATA_API_KEY:
        logger.error('Collection requested but THETADATA_API_KEY is missing; no provider requests')
    if config.PUBLICATION_ENABLED and not publishing:
        logger.warning('Publication requested but collection/channel requirements are not met')


async def run_bot():
    if not config.DISCORD_TOKEN:
        raise RuntimeError('DISCORD_TOKEN is required; configure the private bot before starting')
    bot = build_bot()

    @bot.event
    async def on_ready():
        logger.info('Discord connected | SPY → SPX %s | completed-candle setups; manual execution', VERSION)

    async with bot:
        await install_service(bot)
        await bot.start(config.DISCORD_TOKEN)
