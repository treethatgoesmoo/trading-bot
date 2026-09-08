"""Discord-only presentation for derived SPX context; no raw market rows."""
import discord

from core.market_calendar import ET
from zero_dte.orb_audit import utc


def context_embed(assessment: dict) -> discord.Embed:
    direction = assessment.get("direction", "NEUTRAL")
    colors = {"UP": 0x2ECC71, "DOWN": 0xE74C3C, "NEUTRAL": 0xF1C40F}
    embed = discord.Embed(
        title="SPX structure · " + assessment["setup"]["state"].replace("_", " ").title(),
        description="Experimental action checklist + observed SPX context · no contract selection or proven edge",
        color=colors.get(direction, colors["NEUTRAL"]),
    )
    from zero_dte.spx_actions import validated_action
    action = validated_action(assessment)
    embed.add_field(name='Action', value=action['text'][:1024] if action else
                    'NO TRADE\nAction checklist unavailable for this snapshot.', inline=False)
    for key in ("structure", "setup", "momentum", "confirmation", "invalidation"):
        embed.add_field(name=key.title(), value=assessment[key]["text"][:1024] or "Unavailable", inline=False)
    observed = utc(assessment["as_of"]).astimezone(ET).strftime("%H:%M:%S ET")
    bar = assessment.get("bar_at")
    bar_text = utc(bar).astimezone(ET).strftime("%H:%M ET") if bar else "missing"
    used = ", ".join(name.replace("_", " ") for name, source in assessment.get("sources", {}).items()
                     if source.get("used")) or "none"
    embed.set_footer(text=(f"As of {observed} · minute-start {bar_text} · "
                           f"{'fresh' if assessment.get('fresh') else 'STALE / unavailable'} · "
                           f"Used: {used} · Audit {'enabled' if assessment.get('audit_enabled') else 'off'}: !audit · "
                           f"outcomes are SPX points, not option P&L")[:2048])
    return embed
