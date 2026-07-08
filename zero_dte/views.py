"""Interactive Discord components for the 0DTE feature.

Views take what they need via constructor args (states dict, cog reference)
instead of module globals so they work inside the cog-based bot.
"""

from datetime import datetime

import discord
import pytz

ET = pytz.timezone("US/Eastern")

from zero_dte.strategy import DirectionForecast, TradeSignal
from zero_dte.tracker import log_trade_decision, log_user_vote


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


class ModifyTradeModal(discord.ui.Modal):
    """Edit entry/stop/target on the active trade."""

    def __init__(self, ticker: str, states: dict):
        super().__init__(title=f"Modify {ticker} Trade")
        self.ticker = ticker
        self.states = states
        st = states.get(ticker)
        trade = st.active_trade if st else None
        self.entry = discord.ui.TextInput(label="Entry", default=str(trade.entry_price) if trade else "")
        self.stop = discord.ui.TextInput(label="Stop Loss", default=str(trade.stop_loss) if trade else "")
        self.target = discord.ui.TextInput(label="Target", default=str(trade.target) if trade else "")
        self.add_item(self.entry)
        self.add_item(self.stop)
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        st = self.states.get(self.ticker)
        if not st or not st.active_trade:
            await interaction.response.send_message("No active trade to modify.", ephemeral=True)
            return
        try:
            st.active_trade.entry_price = round(float(self.entry.value), 2)
            st.active_trade.stop_loss = round(float(self.stop.value), 2)
            st.active_trade.target = round(float(self.target.value), 2)
        except ValueError:
            await interaction.response.send_message("Invalid number — trade unchanged.", ephemeral=True)
            return
        log_trade_decision(self.ticker, {
            "entry": st.active_trade.entry_price,
            "stop": st.active_trade.stop_loss,
            "target": st.active_trade.target,
        }, "MODIFIED", interaction.user.name)
        await interaction.response.send_message(
            f"**{self.ticker}** trade updated: entry ${st.active_trade.entry_price}, "
            f"stop ${st.active_trade.stop_loss}, target ${st.active_trade.target}. "
            f"Outcome tracking will use the new levels.")


class TradeSignalView(discord.ui.View):
    """Take/Skip/Modify buttons on a trade alert."""

    def __init__(self, signal: TradeSignal, states: dict):
        super().__init__(timeout=4 * 3600)
        self.signal = signal
        self.states = states

    def _info(self) -> dict:
        return {"direction": self.signal.direction, "entry": self.signal.entry_price,
                "stop": self.signal.stop_loss, "target": self.signal.target}

    @discord.ui.button(label="✅ Take Trade", style=discord.ButtonStyle.success)
    async def take(self, interaction: discord.Interaction, button: discord.ui.Button):
        log_trade_decision(self.signal.ticker, self._info(), "TAKEN", interaction.user.name)
        await interaction.response.send_message(
            f"Logged: trade TAKEN — {self.signal.ticker} {self.signal.direction} @ ${self.signal.entry_price}.",
            ephemeral=True)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        log_trade_decision(self.signal.ticker, self._info(), "SKIPPED", interaction.user.name)
        await interaction.response.send_message(
            f"Logged: signal SKIPPED on {self.signal.ticker}.", ephemeral=True)

    @discord.ui.button(label="✏️ Modify", style=discord.ButtonStyle.primary)
    async def modify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModifyTradeModal(self.signal.ticker, self.states))


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

    @discord.ui.button(label="🧪 Test", style=discord.ButtonStyle.primary, custom_id="menu:test", row=1)
    async def btn_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.run_test(interaction.channel, self._ticker_for(interaction))

    @discord.ui.button(label="📊 Status", style=discord.ButtonStyle.secondary, custom_id="menu:status", row=1)
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.cog.build_status_embed())

    @discord.ui.button(label="🧭 Bias", style=discord.ButtonStyle.secondary, custom_id="menu:bias", row=2)
    async def btn_bias(self, interaction: discord.Interaction, button: discord.ui.Button):
        from zero_dte.embeds import bias_embed
        ticker = self._ticker_for(interaction)
        st = self.cog.states.get(ticker)
        if st and st.daily_bias:
            await interaction.response.send_message(embed=bias_embed(st.daily_bias))
        else:
            await interaction.response.send_message(
                f"No bias set for {ticker} yet — it's computed after the first "
                f"5-min candle (9:35 AM ET). Use 🧪 Test for a dry run.", ephemeral=True)

    @discord.ui.button(label="📦 Range", style=discord.ButtonStyle.secondary, custom_id="menu:range", row=2)
    async def btn_range(self, interaction: discord.Interaction, button: discord.ui.Button):
        from zero_dte.embeds import range_embed
        ticker = self._ticker_for(interaction)
        st = self.cog.states.get(ticker)
        if st and st.first_candle:
            await interaction.response.send_message(embed=range_embed(st.first_candle))
        else:
            await interaction.response.send_message(
                f"No first candle set for {ticker} yet (available after 9:35 AM ET).",
                ephemeral=True)

    @discord.ui.button(label="📋 Watchlist", style=discord.ButtonStyle.secondary, custom_id="menu:watchlist", row=2)
    async def btn_watchlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**Watchlist:** {', '.join(self.cog.active_tickers)}", ephemeral=True)

    @discord.ui.button(label="📈 Swing", style=discord.ButtonStyle.primary, custom_id="menu:swing", row=3)
    async def btn_swing(self, interaction: discord.Interaction, button: discord.ui.Button):
        swing_cog = self.cog.bot.get_cog("Swing")
        if swing_cog is None:
            await interaction.response.send_message("Swing feature is disabled.", ephemeral=True)
            return
        await interaction.response.defer()
        ctx = interaction.channel
        await swing_cog.cmd_swing.callback(swing_cog, ctx, self._ticker_for(interaction))
