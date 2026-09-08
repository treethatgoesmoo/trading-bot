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
    THETA_AUDIT_START_DATE,
    THETA_AUDIT_DAYS,
    THETA_AUDIT_FLOW_ENABLED,
    ZERO_DTE_ENABLED,
    NOTIFICATION_MODE,
    SPX_CONTEXT_FLOW_ENABLED,
    SPX_CONTEXT_CHANNEL_ID,
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

    if NOTIFICATION_MODE not in {"spx", "legacy"}:
        logger.error("Invalid NOTIFICATION_MODE; refusing to enable any trading stream")
        return
    bot = build_bot()
    client = MarketDataClient()

    @bot.event
    async def on_ready():
        logger.info("Bot connected as %s (ID: %s) | Version: %s", bot.user, bot.user.id, get_version())
        logger.info("Config: channel=%s poll=%ds | features: zero_dte=%s swing=%s spx_flow=%s",
                    ALERT_CHANNEL_ID, POLL_INTERVAL_SECONDS, ZERO_DTE_ENABLED,
                    SWING_ENABLED, SPX_FLOW_ENABLED)
        logger.info("Notification mode=%s; SPX private channel=%s", NOTIFICATION_MODE, SPX_CONTEXT_CHANNEL_ID)
        logger.info("Data sources: TwelveData=%s Finnhub=%s yfinance=always",
                    "configured" if client.twelvedata_limiter else "not set",
                    "configured" if client.finnhub_limiter else "not set")

    async with bot:
        from bot.shared_cog import SharedCog
        await bot.add_cog(SharedCog(bot, client))
        if NOTIFICATION_MODE == "spx":
            # Replacement mode overrides old .env toggles; no legacy fallback.
            if not THETADATA_API_KEY:
                logger.error("SPX context requires THETADATA_API_KEY; legacy alerts remain disabled")
            else:
                from core.thetadata_client import ThetaDataClient
                from zero_dte.spx_context_cog import SPXContextCog
                if SPX_CONTEXT_FLOW_ENABLED:
                    from zero_dte.flow_cog import SPXFlowCog
                    await bot.add_cog(SPXFlowCog(
                        bot, client, ThetaDataClient(purpose="spx_context_flow"),
                        audit_only=True, context_session=True,
                    ))
                await bot.add_cog(SPXContextCog(bot, ThetaDataClient(purpose="spx_context")))
                logger.info("Unified SPX stream installed; one index collector feeds context and ORB audit. "
                            "Options sampling/telemetry require explicit audit dates; live index collection continues.")
                if not SPX_CONTEXT_CHANNEL_ID:
                    logger.warning("SPX_CONTEXT_CHANNEL_ID is unset: collecting locally, no notifications")
        if NOTIFICATION_MODE == "legacy" and ZERO_DTE_ENABLED:
            from zero_dte.cog import ZeroDTECog
            await bot.add_cog(ZeroDTECog(bot, client))
        if NOTIFICATION_MODE == "legacy" and SWING_ENABLED:
            from swing.cog import SwingCog
            await bot.add_cog(SwingCog(bot, client))
        if NOTIFICATION_MODE == "legacy" and SPX_FLOW_ENABLED:
            if not THETADATA_API_KEY:
                logger.error("SPX flow watch enabled but THETADATA_API_KEY is missing; feature not started")
            else:
                from core.thetadata_client import ThetaDataClient
                from zero_dte.flow_cog import SPXFlowCog
                await bot.add_cog(SPXFlowCog(bot, client, ThetaDataClient(purpose="spx_flow")))
        if NOTIFICATION_MODE == "legacy" and THETA_AUDIT_START_DATE:
            from datetime import date
            from core.theta_usage import audit_active
            from zero_dte.orb_audit import session_time
            try:
                start = date.fromisoformat(THETA_AUDIT_START_DATE)
                valid_window = audit_active(session_time(start, 9, 30))
            except (ValueError, OverflowError):
                valid_window = False
            if not valid_window:
                logger.error("Invalid Theta audit date/duration; audit sampler not installed")
            elif not THETADATA_API_KEY:
                logger.error("Theta audit configured but API key missing; audit sampler not installed")
            else:
                from core.thetadata_client import ThetaDataClient
                from zero_dte.audit_cog import AuditCog
                if THETA_AUDIT_FLOW_ENABLED and not SPX_FLOW_ENABLED:
                    from zero_dte.flow_cog import SPXFlowCog
                    await bot.add_cog(SPXFlowCog(
                        bot, client, ThetaDataClient(purpose="audit_flow"), audit_only=True,
                    ))
                await bot.add_cog(AuditCog(bot, ThetaDataClient(purpose="audit")))
                logger.info("Local-only Theta/ORB audit configured: ET start=%s days=%d; "
                            "collection stops at window end, subscriptions unchanged",
                            THETA_AUDIT_START_DATE, THETA_AUDIT_DAYS)
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await client.close()
