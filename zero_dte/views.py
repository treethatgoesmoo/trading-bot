"""Interactive Discord components for the 0DTE feature.

Views take what they need via constructor args (states dict, cog reference)
instead of module globals so they work inside the cog-based bot.

(TradeSignalView/ModifyTradeModal — Take/Skip/Modify buttons on
break-and-retest trade alerts — were removed along with that feature. The
MenuView's Bias/Range/Test buttons were removed for the same reason.)
"""

from datetime import datetime

import discord
import pytz

ET = pytz.timezone("US/Eastern")

from zero_dte.strategy import DirectionForecast
from zero_dte.tracker import log_user_vote


class ForecastView(discord.ui.View):
    """Agree/Disagree buttons on a forecast — votes are scored at end of day."""

    def __init__(self, ticker: str, forecast: DirectionForecast, forecast_time_iso: str):
        super().__init__(timeout=4 * 3600)
        self.ticker = ticker
        self.forecast = forecast
        self.forecast_time_iso = forecast_time_iso

    @discord.ui.button(label="👍 Agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        # A NEUTRAL agreement isn't a directional call — log it unscored
        # (actuals are only ever BULLISH/BEARISH, so scoring it guarantees a miss).
        user_dir = self.forecast.direction if self.forecast.direction in ("BULLISH", "BEARISH") else None
        log_user_vote(self.ticker, self.forecast_time_iso, self.forecast.direction,
                      user_dir, interaction.user.name)
        if user_dir:
            msg = f"Logged: you agree with **{user_dir}** on {self.ticker}. Scored at 4 PM."
        else:
            msg = f"Logged your agree on a {self.forecast.direction} forecast (not scored)."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="👎 Disagree", style=discord.ButtonStyle.danger)
    async def disagree(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.forecast.direction == "BULLISH":
            user_dir = "BEARISH"
        elif self.forecast.direction == "BEARISH":
            user_dir = "BULLISH"
        else:
            user_dir = None
        log_user_vote(self.ticker, self.forecast_time_iso, self.forecast.direction,
                      user_dir, interaction.user.name)
        if user_dir:
            msg = f"Logged: you disagree — your call is **{user_dir}** on {self.ticker}. Scored at 4 PM."
        else:
            msg = f"Logged your disagree on a {self.forecast.direction} forecast (not scored)."
        await interaction.response.send_message(msg, ephemeral=True)


class ReportDetailsView(discord.ui.View):
    """View Details button on the daily forecast report card."""

    def __init__(self, ticker: str, date_str: str, results: list[dict]):
        super().__init__(timeout=86400)
        self.ticker = ticker
        self.date_str = date_str
        self.results = results

    @discord.ui.button(label="🔎 View Details", style=discord.ButtonStyle.primary)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=f"Forecast Details — {self.ticker} ({self.date_str})",
            color=0x5865F2,
        )
        lines = []
        for r in self.results:
            ftime = datetime.fromisoformat(r["time"]).astimezone(ET).strftime("%I:%M %p")
            if r.get("correct") is None:
                icon = "\U0001f7e1"
            elif r["correct"]:
                icon = "✅"
            else:
                icon = "❌"
            sigs = " ".join(f"{k}={v[:4]}" for k, v in r.get("signals", {}).items())
            lines.append(f"{icon} **{ftime}** {r['direction']} ({r['confidence']}%) -> {r['actual']} ({r['move']:+.2f})\n   {sigs}")
        for i in range(0, len(lines), 10):
            embed.add_field(name=f"Forecasts {i + 1}-{min(i + 10, len(lines))}",
                            value="\n".join(lines[i:i + 10])[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MenuView(discord.ui.View):
    """Persistent control panel with buttons for common commands.

    Holds a reference to the ZeroDTECog for state and actions; registered as a
    persistent view on ready so buttons on old messages keep working.
    """

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        tickers = cog.active_tickers
        self.default_ticker = tickers[0] if tickers else "SPY"
        # Selection is per-user: one persistent instance serves every old menu
        # message after a restart, so a shared attribute would let one user's
        # pick silently redirect another's buttons.
        self._selected: dict[int, str] = {}
        if tickers:
            select = discord.ui.Select(
                placeholder=f"Ticker (default: {self.default_ticker})",
                options=[discord.SelectOption(label=t) for t in tickers[:25]],
                custom_id="menu:ticker",
            )
            select.callback = self._on_ticker
            self.add_item(select)

    def _ticker_for(self, interaction: discord.Interaction) -> str:
        return self._selected.get(interaction.user.id, self.default_ticker)

    async def _on_ticker(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        self._selected[interaction.user.id] = choice
        await interaction.response.send_message(
            f"Menu ticker set to **{choice}** (for you)", ephemeral=True)

    @discord.ui.button(label="🔮 Forecast", style=discord.ButtonStyle.primary, custom_id="menu:forecast", row=1)
    async def btn_forecast(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.send_forecast(interaction.channel, self._ticker_for(interaction))

    @discord.ui.button(label="📊 Status", style=discord.ButtonStyle.secondary, custom_id="menu:status", row=1)
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.cog.build_status_embed())

    @discord.ui.button(label="📋 Watchlist", style=discord.ButtonStyle.secondary, custom_id="menu:watchlist", row=1)
    async def btn_watchlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**Watchlist:** {', '.join(self.cog.active_tickers)}", ephemeral=True)

    @discord.ui.button(label="📈 Swing", style=discord.ButtonStyle.primary, custom_id="menu:swing", row=2)
    async def btn_swing(self, interaction: discord.Interaction, button: discord.ui.Button):
        swing_cog = self.cog.bot.get_cog("Swing")
        if swing_cog is None:
            await interaction.response.send_message("Swing feature is disabled.", ephemeral=True)
            return
        await interaction.response.defer()
        ctx = interaction.channel
        await swing_cog.cmd_swing.callback(swing_cog, ctx, self._ticker_for(interaction))
