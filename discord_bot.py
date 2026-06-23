import asyncio
import json
import logging
import os
import subprocess
import discord
from discord.ext import commands, tasks
from datetime import datetime
import pytz

from config import (
    DISCORD_TOKEN,
    ALERT_CHANNEL_ID,
    COMMAND_PREFIX,
    TICKERS,
    FIRST_CANDLE_READY_MINUTE,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    MONITORING_END_HOUR,
    MONITORING_END_MINUTE,
    POLL_INTERVAL_SECONDS,
    ATR_OVEREXTENDED_MULT,
    FILTER_COOLDOWN_MINUTES,
    TRADE_TRACKING_END_HOUR,
    TRADE_TRACKING_END_MINUTE,
    ECON_EVENTS_ENABLED,
    ECON_LOOKAHEAD_DAYS,
    FORECAST_INTERVAL_MINUTES,
    FORECAST_START_HOUR,
    FORECAST_START_MINUTE,
    FORECAST_END_HOUR,
    FORECAST_END_MINUTE,
    RSI_WATCH_ENABLED,
    RSI_WATCH_INTERVAL_SECONDS,
)
from strategy import (
    get_daily_bias,
    set_bias_from_first_candle,
    get_first_candle_range,
    get_premarket_levels,
    check_for_setup,
    check_trade_outcome,
    compute_atr,
    compute_30m_forecast,
    StrategyState,
    DailyBias,
    FirstCandleRange,
    PremarketLevels,
    TradeSignal,
    FilteredSignal,
    TradeOutcome,
    DirectionForecast,
)
from market_data import MarketDataClient, is_market_open
from economic_events import fetch_events, EconEvent
from forecast_tracker import (
    log_forecast,
    evaluate_forecasts,
    log_user_vote,
    log_trade_decision,
    evaluate_votes,
    log_trade_outcome,
    log_bias,
    log_filters,
    evaluate_bias,
)
import analytics
import dashboard as dashboard_gen
from rsi_watcher import RSIWatcher, RSIAlert, TF_LABEL
from forecast_tracker import _append_jsonl as _append_data

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# Tracks the day the economic-event briefing was last posted (once per day).
_econ_briefing_date: str | None = None

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), ".watchlist.json")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
active_tickers: list[str] = list(TICKERS)
states: dict[str, StrategyState] = {t: StrategyState() for t in active_tickers}
client = MarketDataClient()

# RSI reversal watchers (one per ticker) + throttle timestamp for the watch loop.
rsi_watchers: dict[str, RSIWatcher] = {}
_rsi_last_run: datetime | None = None


def _get_version() -> str:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return f"{branch}@{commit}"
    except Exception:
        return "unknown"


BOT_VERSION = _get_version()


def _load_watchlist():
    """Load saved watchlist and merge with env tickers."""
    global active_tickers, states
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                saved = json.load(f)
            for t in saved:
                if t not in active_tickers:
                    active_tickers.append(t)
                    states[t] = StrategyState()
        except Exception:
            pass


def _save_watchlist():
    """Persist current watchlist to disk."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(active_tickers, f)
    except Exception:
        pass


_load_watchlist()
rsi_watchers = {t: RSIWatcher(t) for t in active_tickers}


# ── Embed builders ──────────────────────────────────────────

def bias_embed(bias: DailyBias) -> discord.Embed:
    color = 0x00CC66 if bias.direction == "BULLISH" else 0xFF4444
    emoji = "\U0001f7e2" if bias.direction == "BULLISH" else "\U0001f534"
    trend_emoji = "\U0001f7e2" if bias.trend == "BULLISH" else "\U0001f534"
    gap_emoji = "\U0001f7e2" if bias.gap_direction == "GAP_UP" else ("\U0001f534" if bias.gap_direction == "GAP_DOWN" else "\U0001f7e1")
    md_emoji = "\U0001f7e2" if bias.multi_day == "BULLISH" else ("\U0001f534" if bias.multi_day == "BEARISH" else "\U0001f7e1")

    embed = discord.Embed(title=f"{emoji} Daily Bias: {bias.direction}", color=color)
    embed.add_field(name="Ticker", value=bias.ticker, inline=True)
    embed.add_field(name="Gap", value=f"{gap_emoji} {bias.gap_direction} ({bias.gap_pct:+.2f}%)", inline=True)
    embed.add_field(name="Prev Day Range", value=f"${bias.prev_low} - ${bias.prev_high}", inline=True)
    embed.add_field(name="5-Day Trend", value=f"{md_emoji} {bias.multi_day} ({bias.green_days}G / {bias.red_days}R)", inline=True)
    embed.add_field(name="50 SMA", value=f"{trend_emoji} {bias.trend} (${bias.current_price} vs ${bias.sma_value})", inline=True)
    rsi_str = f"{bias.rsi} ({bias.rsi_signal})" if bias.rsi is not None else "N/A"
    embed.add_field(name="Daily RSI", value=rsi_str, inline=True)
    embed.set_footer(text="Bias: first candle direction + gap + RSI contrarian")
    return embed


def forecast_embed(fc: DirectionForecast) -> discord.Embed:
    if fc.direction == "BULLISH":
        color, emoji = 0x00CC66, "\U0001f7e2"
    elif fc.direction == "BEARISH":
        color, emoji = 0xFF4444, "\U0001f534"
    else:
        color, emoji = 0xFFAA00, "\U0001f7e1"

    embed = discord.Embed(
        title=f"{emoji} 30-Min Forecast: {fc.direction} {fc.ticker}",
        color=color,
    )
    embed.add_field(name="Confidence", value=f"{fc.confidence}%", inline=True)
    embed.add_field(name="Price", value=f"${fc.current_price}", inline=True)
    if fc.vwap:
        pos = "above" if fc.current_price > fc.vwap else "below"
        embed.add_field(name="VWAP", value=f"${fc.vwap} ({pos})", inline=True)
    if fc.rsi_30m:
        embed.add_field(name="RSI", value=f"{fc.rsi_30m}", inline=True)
    if fc.ema_direction:
        embed.add_field(name="EMA 9/21", value=fc.ema_direction, inline=True)
    if fc.macd_direction:
        embed.add_field(name="MACD", value=fc.macd_direction, inline=True)
    if fc.divergence:
        div_str = "Bearish divergence" if fc.divergence == "BEARISH_DIV" else "Bullish divergence"
        embed.add_field(name="Divergence", value=f"⚠️ {div_str}", inline=True)

    # Signal breakdown
    sig_lines = []
    for name, val in fc.signals.items():
        w = fc.weights.get(name, 0)
        icon = "\U0001f7e2" if val == "BULLISH" else ("\U0001f534" if val == "BEARISH" else "\U0001f7e1")
        sig_lines.append(f"{icon} {name} (w:{w})")
    if sig_lines:
        embed.add_field(name="Signals", value=" | ".join(sig_lines), inline=False)

    embed.set_footer(text=f"Forecast at {fc.timestamp} | Next 30 min outlook")
    return embed


def forecast_results_embed(ticker: str, date_str: str, results: list[dict],
                           user_score: tuple[int, int] | None = None) -> discord.Embed:
    scored = [r for r in results if r.get("correct") is not None]
    correct = sum(1 for r in scored if r["correct"])
    total = len(scored)
    accuracy = correct / total * 100 if total else 0

    if accuracy >= 60:
        color = 0x00CC66
    elif accuracy >= 45:
        color = 0xFFAA00
    else:
        color = 0xFF4444

    embed = discord.Embed(
        title=f"\U0001f4c8 Forecast Report Card — {ticker} ({date_str})",
        color=color,
    )
    embed.add_field(name="Bot Accuracy", value=f"{correct}/{total} ({accuracy:.0f}%)", inline=True)
    if user_score and user_score[1] > 0:
        u_correct, u_total = user_score
        embed.add_field(name="Your Calls", value=f"{u_correct}/{u_total} ({u_correct / u_total * 100:.0f}%)", inline=True)

    lines = []
    for r in results:
        ftime = datetime.fromisoformat(r["time"]).strftime("%I:%M %p")
        if r.get("correct") is None:
            icon = "\U0001f7e1"
        elif r["correct"]:
            icon = "✅"
        else:
            icon = "❌"
        lines.append(f"{icon} {ftime}: {r['direction']} ({r['confidence']}%) -> {r['actual']} ({r['move']:+.2f})")
    if lines:
        embed.add_field(name="Forecasts", value="\n".join(lines[:10]), inline=False)

    embed.set_footer(text=f"Saved to data/results-{date_str}.json | Click View Details for full breakdown")
    return embed


def range_embed(fc: FirstCandleRange) -> discord.Embed:
    embed = discord.Embed(title=f"\U0001f4e6 First 5-Min Candle Range — {fc.ticker}", color=0x3498DB)
    embed.add_field(name="Ticker", value=fc.ticker, inline=True)
    embed.add_field(name="High (Resistance)", value=f"${fc.high}", inline=True)
    embed.add_field(name="Low (Support)", value=f"${fc.low}", inline=True)
    embed.add_field(name="Range Size", value=f"${fc.range_size:.2f}", inline=True)
    embed.add_field(name="Candle Type", value=fc.candle_type, inline=True)
    embed.set_footer(text="9:30 - 9:35 AM ET | Do NOT trade inside this range")
    return embed


def premarket_embed(pm: PremarketLevels) -> discord.Embed:
    embed = discord.Embed(title=f"\U0001f4ca Pre-Market Levels — {pm.ticker}", color=0x9B59B6)
    embed.add_field(name="PM High", value=f"${pm.high}", inline=True)
    embed.add_field(name="PM Low", value=f"${pm.low}", inline=True)
    embed.add_field(name="PM Volume", value=f"{pm.volume:,.0f}", inline=True)
    embed.set_footer(text="Pre-market session (4:00 AM - 9:30 AM ET)")
    return embed


def signal_embed(signal: TradeSignal) -> discord.Embed:
    color = 0x00CC66 if signal.direction == "LONG" else 0xFF4444
    emoji = "\U0001f7e2" if signal.direction == "LONG" else "\U0001f534"
    arrow = "⬆️" if signal.direction == "LONG" else "⬇️"
    embed = discord.Embed(
        title=f"{emoji} {arrow} TRADE ALERT: {signal.direction} {signal.ticker}",
        color=color,
    )
    embed.add_field(name="Entry", value=f"${signal.entry_price}", inline=True)
    embed.add_field(name="Stop Loss", value=f"${signal.stop_loss}", inline=True)
    embed.add_field(name="Target (2R)", value=f"${signal.target}", inline=True)
    embed.add_field(name="Risk", value=f"${signal.risk}", inline=True)
    embed.add_field(name="Reward", value=f"${signal.reward}", inline=True)
    embed.add_field(name="R:R", value=f"1:{int(signal.reward / signal.risk) if signal.risk else 0}", inline=True)
    if signal.filters:
        filter_lines = [f"✅ {f.name}: {f.reason}" for f in signal.filters]
        embed.add_field(name="Filters", value="\n".join(filter_lines), inline=False)
    embed.set_footer(text=f"Signal at {signal.timestamp} | First Candle Strategy")
    return embed


def filtered_embed(fs: FilteredSignal) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ Setup Filtered: {fs.direction} {fs.ticker}",
        color=0xFFA500,
    )
    embed.add_field(name="Entry (would be)", value=f"${fs.entry_price}", inline=True)
    embed.add_field(name="Time", value=fs.timestamp, inline=True)
    embed.add_field(name="Cooldown", value=f"{FILTER_COOLDOWN_MINUTES} min", inline=True)
    lines = []
    for f in fs.filters:
        icon = "✅" if f.passed else "❌"
        lines.append(f"{icon} **{f.name}**: {f.reason}")
    embed.add_field(name="Filter Results", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Will retry after {FILTER_COOLDOWN_MINUTES} min cooldown")
    return embed


def overextended_embed(ticker: str, range_size: float, atr: float) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ {ticker} — Opening Range Overextended",
        color=0xFFA500,
    )
    embed.add_field(name="First Candle Range", value=f"${range_size:.2f}", inline=True)
    embed.add_field(name="ATR (14-period)", value=f"${atr:.2f}", inline=True)
    embed.add_field(name="Threshold", value=f"{ATR_OVEREXTENDED_MULT}x ATR = ${atr * ATR_OVEREXTENDED_MULT:.2f}", inline=True)
    embed.set_footer(text=f"Skipping {ticker} for today")
    return embed


def outcome_embed(outcome: TradeOutcome) -> discord.Embed:
    if outcome.result == "TARGET_HIT":
        color = 0x00CC66
        icon = "✅"
        title = f"{icon} TARGET HIT: {outcome.direction} {outcome.ticker}"
    else:
        color = 0xFF4444
        icon = "❌"
        title = f"{icon} STOPPED OUT: {outcome.direction} {outcome.ticker}"

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Entry", value=f"${outcome.entry_price}", inline=True)
    embed.add_field(name="Exit", value=f"${outcome.exit_price}", inline=True)
    embed.add_field(name="P/L", value=f"${outcome.pnl:+.2f}", inline=True)
    embed.add_field(name="Stop", value=f"${outcome.stop_loss}", inline=True)
    embed.add_field(name="Target", value=f"${outcome.target}", inline=True)
    embed.set_footer(text=f"Closed at {outcome.timestamp}")
    return embed


def daily_summary_embed(today_str: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"\U0001f4cb Daily Summary — {today_str}",
        color=0x2ECC71,
    )
    for ticker in active_tickers:
        st = states.get(ticker)
        if st is None:
            continue

        lines = []
        if st.daily_bias:
            lines.append(f"Bias: {st.daily_bias.direction} ({st.daily_bias.strength})")
        if st.first_candle:
            lines.append(f"Range: ${st.first_candle.high} / ${st.first_candle.low} (${st.first_candle.range_size:.2f})")
        if st.first_candle_overextended:
            lines.append("Skipped: Overextended opening")
        elif st.trade_outcome:
            o = st.trade_outcome
            icon = "✅" if o.result == "TARGET_HIT" else "❌"
            lines.append(f"{icon} {o.direction}: ${o.entry_price} -> ${o.exit_price} (${o.pnl:+.2f})")
        elif st.active_trade:
            lines.append(f"Open: {st.active_trade.direction} @ ${st.active_trade.entry_price}")
        elif st.signal_fired:
            lines.append("Signal fired (no outcome tracked)")
        else:
            lines.append("No signal")

        if st.filtered_signals:
            lines.append(f"Filtered setups: {len(st.filtered_signals)}")

        embed.add_field(
            name=f"── {ticker} ──",
            value="\n".join(lines) if lines else "No activity",
            inline=True,
        )

    return embed


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


async def maybe_post_econ_briefing(channel, today_str: str) -> None:
    """Post the economic-event briefing once per trading day. Best-effort."""
    global _econ_briefing_date
    if not ECON_EVENTS_ENABLED or channel is None:
        return
    if _econ_briefing_date == today_str:
        return
    _econ_briefing_date = today_str  # set first so a failure doesn't retry every poll
    try:
        events = await fetch_events()
        logger.info("Economic briefing: %d upcoming event(s) within %d days",
                    len(events), ECON_LOOKAHEAD_DAYS)
        await channel.send(embed=economic_events_embed(events))
    except Exception as exc:
        logger.warning("Economic briefing failed: %s", exc)


_RSI_KIND_LABEL = {
    "THRESHOLD": "RSI Threshold",
    "DIVERGENCE": "RSI Divergence",
    "FAILURE_SWING": "RSI Failure Swing",
}


def rsi_alert_embed(alert: RSIAlert) -> discord.Embed:
    """A reversal-watcher ping for a single RSI signal."""
    if alert.direction == "BULLISH":
        color, arrow = 0x00CC66, "⬆️"
    else:
        color, arrow = 0xFF4444, "⬇️"
    tier = f" [{alert.tier.upper()}]" if alert.tier else ""
    kind = _RSI_KIND_LABEL.get(alert.kind, alert.kind)
    title = f"\U0001f514 {arrow} {kind}{tier} — {alert.ticker} ({TF_LABEL[alert.timeframe]})"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Signal", value=alert.message, inline=False)
    embed.add_field(name="RSI", value=f"{alert.rsi}", inline=True)
    embed.add_field(name="Implied", value=f"{alert.direction} reversal ({alert.stage})", inline=True)
    if alert.confirmed:
        embed.add_field(name="Confirmation", value="✅ 5m + 15m agree", inline=True)
    embed.set_footer(text="RSI reversal watcher — confirm with price action before acting")
    return embed


async def run_rsi_watch(channel) -> None:
    """Poll every watched ticker's RSI on 5m+15m and post any reversal pings."""
    if not RSI_WATCH_ENABLED:
        return
    for ticker in list(active_tickers):
        watcher = rsi_watchers.get(ticker)
        if watcher is None:
            watcher = rsi_watchers[ticker] = RSIWatcher(ticker)
        try:
            alerts = await watcher.check(client)
        except Exception as exc:
            logger.warning("[%s] RSI watch error: %s", ticker, exc)
            continue
        for alert in alerts:
            logger.info("[%s] RSI alert: %s %s %s (%s) conf=%s | %s",
                        ticker, alert.timeframe, alert.kind, alert.direction,
                        alert.rsi, alert.confirmed, alert.message)
            try:
                _append_data("rsi_alerts", datetime.now(ET).strftime("%Y-%m-%d"), {
                    "ticker": ticker, "time": datetime.now(ET).isoformat(),
                    "timeframe": alert.timeframe, "kind": alert.kind,
                    "direction": alert.direction, "stage": alert.stage,
                    "tier": alert.tier, "rsi": alert.rsi, "confirmed": alert.confirmed,
                })
            except Exception:
                pass
            if channel:
                content = "\U0001f514 **Confirmed RSI reversal**" if alert.confirmed else None
                await channel.send(content, embed=rsi_alert_embed(alert))


# ── Interactive components ──────────────────────────────────

class ForecastView(discord.ui.View):
    """Agree/Disagree buttons on a forecast — votes are scored at end of day."""

    def __init__(self, ticker: str, forecast: DirectionForecast, forecast_time_iso: str):
        super().__init__(timeout=4 * 3600)
        self.ticker = ticker
        self.forecast = forecast
        self.forecast_time_iso = forecast_time_iso

    @discord.ui.button(label="👍 Agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        log_user_vote(self.ticker, self.forecast_time_iso, self.forecast.direction,
                      self.forecast.direction, interaction.user.name)
        await interaction.response.send_message(
            f"Logged: you agree with **{self.forecast.direction}** on {self.ticker}. Scored at 4 PM.",
            ephemeral=True)

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
            msg = "Logged your disagree on a NEUTRAL forecast (not scored)."
        await interaction.response.send_message(msg, ephemeral=True)


class ModifyTradeModal(discord.ui.Modal):
    """Edit entry/stop/target on the active trade."""

    def __init__(self, ticker: str):
        super().__init__(title=f"Modify {ticker} Trade")
        self.ticker = ticker
        st = states.get(ticker)
        trade = st.active_trade if st else None
        self.entry = discord.ui.TextInput(label="Entry", default=str(trade.entry_price) if trade else "")
        self.stop = discord.ui.TextInput(label="Stop Loss", default=str(trade.stop_loss) if trade else "")
        self.target = discord.ui.TextInput(label="Target", default=str(trade.target) if trade else "")
        self.add_item(self.entry)
        self.add_item(self.stop)
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        st = states.get(self.ticker)
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

    def __init__(self, signal: TradeSignal):
        super().__init__(timeout=4 * 3600)
        self.signal = signal

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
        await interaction.response.send_modal(ModifyTradeModal(self.signal.ticker))


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
            ftime = datetime.fromisoformat(r["time"]).strftime("%I:%M %p")
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
    """Persistent control panel with buttons for common commands."""

    def __init__(self):
        super().__init__(timeout=None)
        self.selected_ticker = active_tickers[0] if active_tickers else "SPY"
        if active_tickers:
            select = discord.ui.Select(
                placeholder=f"Ticker (default: {self.selected_ticker})",
                options=[discord.SelectOption(label=t) for t in active_tickers[:25]],
                custom_id="menu:ticker",
            )
            select.callback = self._on_ticker
            self.add_item(select)

    async def _on_ticker(self, interaction: discord.Interaction):
        self.selected_ticker = interaction.data["values"][0]
        await interaction.response.send_message(
            f"Menu ticker set to **{self.selected_ticker}**", ephemeral=True)

    @discord.ui.button(label="🔮 Forecast", style=discord.ButtonStyle.primary, custom_id="menu:forecast", row=1)
    async def btn_forecast(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _send_forecast(interaction.channel, self.selected_ticker)

    @discord.ui.button(label="🧪 Test", style=discord.ButtonStyle.primary, custom_id="menu:test", row=1)
    async def btn_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _run_test(interaction.channel, self.selected_ticker)

    @discord.ui.button(label="📊 Status", style=discord.ButtonStyle.secondary, custom_id="menu:status", row=1)
    async def btn_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=build_status_embed())

    @discord.ui.button(label="🧭 Bias", style=discord.ButtonStyle.secondary, custom_id="menu:bias", row=2)
    async def btn_bias(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = states.get(self.selected_ticker)
        if st and st.daily_bias:
            await interaction.response.send_message(embed=bias_embed(st.daily_bias))
        else:
            await interaction.response.send_message(
                f"No bias set for {self.selected_ticker} yet — it's computed after the first "
                f"5-min candle (9:35 AM ET). Use 🧪 Test for a dry run.", ephemeral=True)

    @discord.ui.button(label="📦 Range", style=discord.ButtonStyle.secondary, custom_id="menu:range", row=2)
    async def btn_range(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = states.get(self.selected_ticker)
        if st and st.first_candle:
            await interaction.response.send_message(embed=range_embed(st.first_candle))
        else:
            await interaction.response.send_message(
                f"No first candle set for {self.selected_ticker} yet (available after 9:35 AM ET).",
                ephemeral=True)

    @discord.ui.button(label="📋 Watchlist", style=discord.ButtonStyle.secondary, custom_id="menu:watchlist", row=2)
    async def btn_watchlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**Watchlist:** {', '.join(active_tickers)}", ephemeral=True)


async def _send_forecast(dest, ticker: str):
    """Compute, log, and post an on-demand 30-min forecast with vote buttons."""
    fc = await compute_30m_forecast(client, ticker)
    if fc:
        now = datetime.now(ET)
        log_forecast(ticker, fc, now)
        await dest.send(embed=forecast_embed(fc), view=ForecastView(ticker, fc, now.isoformat()))
    else:
        await dest.send(f"Could not compute forecast for {ticker} (insufficient data).")


# ── Bot events ──────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("Bot connected as %s (ID: %s) | Version: %s", bot.user, bot.user.id, BOT_VERSION)
    logger.info("Config: tickers=%s channel=%s poll=%ds window=%02d:%02d-%02d:%02d ET",
                ",".join(active_tickers), ALERT_CHANNEL_ID, POLL_INTERVAL_SECONDS,
                MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, MONITORING_END_HOUR, MONITORING_END_MINUTE)
    logger.info("Data sources: TwelveData=%s Finnhub=%s yfinance=always",
                "configured" if client.twelvedata_limiter else "not set",
                "configured" if client.finnhub_limiter else "not set")
    if not getattr(bot, "_menu_view_registered", False):
        bot.add_view(MenuView())
        bot._menu_view_registered = True
    if not market_monitor.is_running():
        market_monitor.start()
        logger.info("Market monitor loop started (every %ds)", POLL_INTERVAL_SECONDS)


# ── Helpers ────────────────────────────────────────────────

def _resolve_ticker(arg: str | None) -> str | None:
    if arg is None:
        return active_tickers[0] if len(active_tickers) == 1 else None
    upper = arg.upper()
    return upper if upper in states else None


# ── Commands ────────────────────────────────────────────────

@bot.command(name="bias")
async def cmd_bias(ctx, ticker: str = None):
    """Get today's daily bias. Usage: !bias [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!bias <{'|'.join(active_tickers)}>`")
        return
    st = states[resolved]
    if st.first_candle is None:
        await ctx.send(f"First 5-min candle hasn't been set for {resolved} yet.")
        return
    await ctx.send(f"Fetching daily bias for {resolved}...")
    try:
        bias = await get_daily_bias(client, resolved)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if bias is None:
        await ctx.send("Could not fetch daily data. Market may be closed.")
        return
    st.daily_bias = bias
    await ctx.send(embed=bias_embed(bias))


@bot.command(name="range")
async def cmd_range(ctx, ticker: str = None):
    """Get today's first 5-minute candle range. Usage: !range [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!range <{'|'.join(active_tickers)}>`")
        return
    st = states[resolved]
    await ctx.send(f"Fetching first candle range for {resolved}...")
    try:
        fc = await get_first_candle_range(client, resolved)
    except Exception as exc:
        await ctx.send(f"Error fetching data: {exc}")
        return
    if fc is None:
        await ctx.send("First 5-min candle not available yet. Wait until after 9:35 AM ET.")
        return
    st.first_candle = fc
    await ctx.send(embed=range_embed(fc))

    if st.daily_bias is None:
        try:
            bias = await get_daily_bias(client, resolved)
        except Exception as exc:
            await ctx.send(f"Error fetching daily bias: {exc}")
            return
        if bias is None:
            await ctx.send("Could not fetch daily bias data.")
            return
        st.daily_bias = bias
        await ctx.send(embed=bias_embed(bias))


def build_status_embed() -> discord.Embed:
    now = datetime.now(ET)
    market = "OPEN" if is_market_open() else "CLOSED"
    monitoring = "Active" if market_monitor.is_running() else "Idle"

    embed = discord.Embed(title="⚙️ Bot Status", color=0x95A5A6)
    embed.add_field(name="Market", value=market, inline=True)
    embed.add_field(name="Monitoring", value=monitoring, inline=True)
    embed.add_field(name="Time (ET)", value=now.strftime("%I:%M %p"), inline=True)
    embed.add_field(name="Version", value=BOT_VERSION, inline=True)
    embed.add_field(name="Tickers", value=", ".join(active_tickers), inline=True)

    for ticker in active_tickers:
        st = states[ticker]
        bias_str = st.daily_bias.direction if st.daily_bias else "Not set"
        candle_str = f"${st.first_candle.high} / ${st.first_candle.low}" if st.first_candle else "Not set"
        atr_str = f"${st.atr_value:.2f}" if st.atr_value else "N/A"
        overext_str = " (OVEREXTENDED)" if st.first_candle_overextended else ""

        status_lines = [
            f"Bias: {bias_str}",
            f"Candle: {candle_str}",
            f"ATR: {atr_str}{overext_str}",
        ]

        if st.trade_outcome:
            o = st.trade_outcome
            icon = "✅" if o.result == "TARGET_HIT" else "❌"
            status_lines.append(f"Trade: {icon} {o.result} (${o.pnl:+.2f})")
        elif st.active_trade:
            status_lines.append(f"Trade: OPEN {st.active_trade.direction} @ ${st.active_trade.entry_price}")
        elif st.signal_fired:
            status_lines.append("Signal: Fired")
        else:
            status_lines.append("Signal: Waiting")

        if st.premarket:
            status_lines.append(f"PM: ${st.premarket.high} / ${st.premarket.low}")

        embed.add_field(name=f"── {ticker} ──", value="\n".join(status_lines), inline=True)

    sources = []
    if client.twelvedata_limiter:
        remaining = client.twelvedata_limiter.remaining_daily
        sources.append(f"TwelveData: {remaining} calls left")
    else:
        sources.append("TwelveData: not configured")
    if client.finnhub_limiter:
        sources.append("Finnhub: active")
    else:
        sources.append("Finnhub: not configured")
    sources.append("yfinance: fallback ready")
    embed.add_field(name="Data Sources", value="\n".join(sources), inline=False)
    return embed


@bot.command(name="status")
async def cmd_status(ctx):
    """Show current bot status for all tickers."""
    await ctx.send(embed=build_status_embed())


@bot.command(name="add")
async def cmd_add(ctx, ticker: str = None):
    """Add a ticker to the watchlist. Usage: !add AAPL"""
    if ticker is None:
        await ctx.send("Usage: `!add <TICKER>`")
        return
    upper = ticker.upper()
    if upper in states:
        await ctx.send(f"{upper} is already being tracked.")
        return
    active_tickers.append(upper)
    states[upper] = StrategyState()
    rsi_watchers[upper] = RSIWatcher(upper)
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    states[upper].trading_day = today_str
    _save_watchlist()
    await ctx.send(f"✅ Added **{upper}** to watchlist. Now tracking: {', '.join(active_tickers)}")


@bot.command(name="remove")
async def cmd_remove(ctx, ticker: str = None):
    """Remove a ticker from the watchlist. Usage: !remove AAPL"""
    if ticker is None:
        await ctx.send("Usage: `!remove <TICKER>`")
        return
    upper = ticker.upper()
    if upper not in states:
        await ctx.send(f"{upper} is not being tracked.")
        return
    active_tickers.remove(upper)
    del states[upper]
    rsi_watchers.pop(upper, None)
    _save_watchlist()
    await ctx.send(f"❌ Removed **{upper}** from watchlist. Now tracking: {', '.join(active_tickers)}")


@bot.command(name="watchlist")
async def cmd_watchlist(ctx):
    """Show current watchlist."""
    await ctx.send(f"**Watchlist:** {', '.join(active_tickers)}")


@bot.command(name="events")
async def cmd_events(ctx, days: int = None):
    """Show upcoming major economic events (Kalshi). Usage: !events [days]"""
    await ctx.send("Fetching economic calendar from Kalshi...")
    try:
        events = await fetch_events(lookahead_days=days)
    except Exception as exc:
        await ctx.send(f"Error fetching economic events: {exc}")
        return
    await ctx.send(embed=economic_events_embed(events))


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

    embed.set_footer(text="!dashboard for the full HTML report")
    return embed


@bot.command(name="stats")
async def cmd_stats(ctx, days: int = None):
    """Show performance stats across history. Usage: !stats [days]"""
    try:
        report = await asyncio.to_thread(analytics.build_report, days)
    except Exception as exc:
        await ctx.send(f"Error building stats: {exc}")
        return
    await ctx.send(embed=stats_embed(report))


@bot.command(name="dashboard")
async def cmd_dashboard(ctx, days: int = None):
    """Generate the full HTML performance dashboard. Usage: !dashboard [days]"""
    await ctx.send("Generating dashboard...")

    def _generate() -> tuple[str, dict]:
        report = analytics.build_report(days=days)
        with open(dashboard_gen.OUT_PATH, "w", encoding="utf-8") as f:
            f.write(dashboard_gen.render(report))
        return dashboard_gen.OUT_PATH, report

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


@bot.command(name="rsi")
async def cmd_rsi(ctx, ticker: str = None):
    """Show current 5m/15m RSI and run the reversal watcher. Usage: !rsi [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!rsi <{'|'.join(active_tickers)}>`")
        return
    from rsi_watcher import rsi_series, TIMEFRAMES
    import pandas as pd

    lines = []
    watcher = rsi_watchers.get(resolved) or RSIWatcher(resolved)
    posted = 0
    for tf in TIMEFRAMES:
        try:
            df = await client.fetch_candles(resolved, interval=tf, count=120)
            r = rsi_series(df["close"].astype(float), watcher.period)
            val = r.iloc[-1]
            zone = ("overbought" if val >= 70 else "oversold" if val <= 30 else "neutral")
            lines.append(f"**{TF_LABEL[tf]}**: RSI {val:.1f} ({zone})")
        except Exception as exc:
            lines.append(f"**{TF_LABEL[tf]}**: error — {exc}")

    embed = discord.Embed(title=f"📈 RSI — {resolved}", color=0x3498DB,
                          description="\n".join(lines))
    embed.set_footer(text="Live watcher pings on threshold/divergence/failure-swing reversals")
    await ctx.send(embed=embed)

    # Also surface any live signal right now.
    try:
        alerts = await watcher.check(client)
        for alert in alerts:
            await ctx.send(embed=rsi_alert_embed(alert))
            posted += 1
    except Exception:
        pass
    if not posted:
        await ctx.send("No active RSI reversal signal at the moment.")


@bot.command(name="test")
async def cmd_test(ctx, ticker: str = None):
    """Simulate the full strategy flow for a ticker. Usage: !test [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!test <{'|'.join(active_tickers)}>`")
        return
    await _run_test(ctx, resolved)


@bot.command(name="menu")
async def cmd_menu(ctx):
    """Show the interactive control panel."""
    embed = discord.Embed(
        title="🤖 Trading Bot Control Panel",
        description="Pick a ticker from the dropdown, then use the buttons.",
        color=0x5865F2,
    )
    await ctx.send(embed=embed, view=MenuView())


@bot.command(name="forecast")
async def cmd_forecast(ctx, ticker: str = None):
    """Get an on-demand 30-min forecast. Usage: !forecast [ticker]"""
    resolved = _resolve_ticker(ticker)
    if resolved is None:
        await ctx.send(f"Specify a ticker: `!forecast <{'|'.join(active_tickers)}>`")
        return
    await ctx.send(f"Computing 30-min forecast for {resolved}...")
    await _send_forecast(ctx, resolved)


async def _run_test(ctx, resolved: str):
    """Full strategy dry run. ctx is anything with an async send() (Context or channel)."""
    await ctx.send(f"**Running full strategy test for {resolved}...**")

    # Step 0: Pre-market levels
    await ctx.send("Fetching pre-market levels...")
    try:
        pm = await get_premarket_levels(resolved)
        if pm:
            await ctx.send(embed=premarket_embed(pm))
        else:
            await ctx.send("No pre-market data available (market may not have had a pre-market session yet).")
    except Exception as exc:
        await ctx.send(f"Pre-market fetch error: {exc}")

    # Step 1: First candle
    await ctx.send("Fetching first 5-min candle...")
    try:
        fc = await get_first_candle_range(client, resolved)
        if fc:
            await ctx.send(embed=range_embed(fc))
        else:
            await ctx.send("First 5-min candle not available. Market may not be open yet or no 9:30 candle found.")
            return
    except Exception as exc:
        await ctx.send(f"First candle error: {exc}")
        return

    # Step 1.5: ATR check
    await ctx.send("Computing ATR...")
    try:
        atr = await compute_atr(client, resolved)
        if atr:
            overextended = fc.range_size > ATR_OVEREXTENDED_MULT * atr
            if overextended:
                await ctx.send(embed=overextended_embed(resolved, fc.range_size, atr))
                return
            else:
                await ctx.send(f"ATR check passed: range ${fc.range_size:.2f} <= {ATR_OVEREXTENDED_MULT}x ATR ${atr:.2f}")
        else:
            await ctx.send("Could not compute ATR (insufficient data).")
    except Exception as exc:
        await ctx.send(f"ATR error: {exc}")

    # Step 2: Daily bias (with first candle direction + gap)
    await ctx.send("Fetching daily bias...")
    try:
        bias = await get_daily_bias(client, resolved, today_open=fc.open_price)
        if bias:
            set_bias_from_first_candle(bias, fc)
            await ctx.send(embed=bias_embed(bias))
        else:
            await ctx.send("Could not fetch daily bias.")
            return
    except Exception as exc:
        await ctx.send(f"Bias error: {exc}")
        return

    # Step 2.5: 30-min forecast
    await ctx.send("Computing 30-min forecast...")
    try:
        forecast = await compute_30m_forecast(client, resolved)
        if forecast:
            await ctx.send(embed=forecast_embed(forecast))
        else:
            await ctx.send("Could not compute forecast (insufficient data).")
    except Exception as exc:
        await ctx.send(f"Forecast error: {exc}")

    # Step 3: Check for setup (using a temporary state so we don't affect the live one)
    await ctx.send("Checking for break-and-retest setup...")
    test_state = StrategyState()
    test_state.trading_day = datetime.now(ET).strftime("%Y-%m-%d")
    test_state.first_candle = fc
    test_state.daily_bias = bias
    test_state.premarket = pm
    if atr:
        test_state.atr_value = atr

    try:
        result = await check_for_setup(test_state, client, resolved)
        if isinstance(result, TradeSignal):
            await ctx.send("**Signal detected!**", embed=signal_embed(result))
        elif isinstance(result, FilteredSignal):
            await ctx.send("**Setup found but filtered:**", embed=filtered_embed(result))
        else:
            await ctx.send("No break-and-retest pattern detected at this time.")
    except Exception as exc:
        await ctx.send(f"Setup check error: {exc}")

    await ctx.send(f"**Test complete for {resolved}.**")


# ── Background monitor ──────────────────────────────────────

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def market_monitor():
    now = datetime.now(ET)
    time_str = now.strftime("%Y-%m-%d %I:%M:%S %p ET")

    if now.weekday() >= 5:
        logger.debug("[%s] Weekend — skipping", time_str)
        return

    today_str = now.strftime("%Y-%m-%d")
    channel = bot.get_channel(ALERT_CHANNEL_ID)

    # Reset state when the trading day changes
    first_state = states[active_tickers[0]] if active_tickers else None
    if first_state and first_state.trading_day != today_str:
        logger.info("[%s] New trading day detected (%s) — resetting state for all tickers", time_str, today_str)
        for ticker in active_tickers:
            states[ticker].reset(today_str)
            rsi_watchers.setdefault(ticker, RSIWatcher(ticker)).reset()

        # Only send "Monitoring started" if we're within the monitoring window (market open to end)
        market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        if market_open_time <= now < monitoring_cutoff:
            for ticker in active_tickers:
                states[ticker].monitoring_started_posted = True
            if channel:
                await channel.send(
                    f"**Monitoring started for {today_str}** | "
                    f"{', '.join(active_tickers)} | {market_open_time.strftime('%I:%M %p')} - {monitoring_cutoff.strftime('%I:%M %p')} ET"
                )
                await maybe_post_econ_briefing(channel, today_str)
            else:
                logger.error("[%s] Channel %s not found — cannot send messages", time_str, ALERT_CHANNEL_ID)
        elif now >= monitoring_cutoff:
            for ticker in active_tickers:
                states[ticker].monitoring_ended = True
            logger.info("[%s] Bot started after monitoring window — skipping session", time_str)
        else:
            logger.info("[%s] Bot started before market open — will post when market opens", time_str)

    # Trade tracking continues until market close
    tracking_cutoff = now.replace(hour=TRADE_TRACKING_END_HOUR, minute=TRADE_TRACKING_END_MINUTE, second=0, microsecond=0)
    past_tracking_cutoff = now >= tracking_cutoff

    # Stop signal monitoring after the cutoff, but track open trades
    monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
    past_monitoring = now >= monitoring_cutoff

    if past_monitoring and first_state and not first_state.monitoring_ended:
        logger.info("[%s] Monitoring window ended", time_str)
        if channel:
            for ticker in active_tickers:
                states[ticker].monitoring_ended = True
            await channel.send(embed=daily_summary_embed(today_str))

    # Expire open trades at tracking cutoff
    if past_tracking_cutoff:
        for ticker in active_tickers:
            st = states[ticker]
            if st.active_trade and not st.trade_outcome:
                st.trade_outcome = TradeOutcome(
                    ticker=ticker, direction=st.active_trade.direction,
                    entry_price=st.active_trade.entry_price,
                    exit_price=st.active_trade.entry_price,
                    stop_loss=st.active_trade.stop_loss,
                    target=st.active_trade.target,
                    result="EXPIRED", pnl=0.0,
                    timestamp=now.strftime("%I:%M %p ET"),
                )
                st.active_trade = None
                log_trade_outcome(st.trade_outcome, now)
                if channel:
                    await channel.send(f"**{ticker}** trade expired at market close — no target or stop hit.")

            # Evaluate today's forecasts + bias against actual outcomes (once per day)
            if not st.forecast_eval_done:
                st.forecast_eval_done = True
                try:
                    await evaluate_bias(client, ticker, today_str)
                except Exception as exc:
                    logger.warning("[%s] [%s] Bias evaluation error: %s", time_str, ticker, exc)
                try:
                    results = await evaluate_forecasts(client, ticker, today_str)
                    if results:
                        user_score = evaluate_votes(ticker, today_str, results)
                        logger.info("[%s] [%s] Forecast evaluation: %d forecasts scored, saved to data/results-%s.json",
                                    time_str, ticker, len(results), today_str)
                        if channel:
                            await channel.send(
                                embed=forecast_results_embed(ticker, today_str, results, user_score),
                                view=ReportDetailsView(ticker, today_str, results),
                            )
                except Exception as exc:
                    logger.exception("[%s] [%s] Forecast evaluation error: %s", time_str, ticker, exc)
        if past_monitoring:
            return

    if channel is None:
        logger.error("[%s] Channel %s not found — skipping poll", time_str, ALERT_CHANNEL_ID)
        return

    # Track active trades (even after monitoring window closes)
    for ticker in list(active_tickers):
        st = states.get(ticker)
        if st is None:
            continue
        if st.active_trade and not st.trade_outcome:
            try:
                outcome = await check_trade_outcome(st, client, ticker)
                if outcome:
                    st.trade_outcome = outcome
                    st.active_trade = None
                    logger.info("[%s] [%s] Trade %s: %s entry=$%s exit=$%s pnl=$%s",
                                time_str, ticker, outcome.result, outcome.direction,
                                outcome.entry_price, outcome.exit_price, outcome.pnl)
                    log_trade_outcome(outcome, now)
                    await channel.send(embed=outcome_embed(outcome))
            except Exception as exc:
                logger.exception("[%s] [%s] Trade tracking error: %s", time_str, ticker, exc)

    # 30-min directional forecasts (run throughout the trading day)
    forecast_start = now.replace(hour=FORECAST_START_HOUR, minute=FORECAST_START_MINUTE, second=0, microsecond=0)
    forecast_end = now.replace(hour=FORECAST_END_HOUR, minute=FORECAST_END_MINUTE, second=0, microsecond=0)
    if forecast_start <= now < forecast_end and now.minute % FORECAST_INTERVAL_MINUTES < 2:
        for ticker in list(active_tickers):
            st = states.get(ticker)
            if st is None:
                continue
            if st.last_forecast_time and (now - st.last_forecast_time).total_seconds() < (FORECAST_INTERVAL_MINUTES * 60 - 30):
                continue
            try:
                fc = await compute_30m_forecast(client, ticker)
                if fc:
                    st.last_forecast_time = now
                    log_forecast(ticker, fc, now)
                    logger.info("[%s] [%s] 30m Forecast: %s (%.1f%%) | RSI=%s EMA=%s MACD=%s Div=%s",
                                time_str, ticker, fc.direction, fc.confidence,
                                fc.rsi_30m, fc.ema_direction, fc.macd_direction, fc.divergence)
                    if channel:
                        await channel.send(embed=forecast_embed(fc), view=ForecastView(ticker, fc, now.isoformat()))
            except Exception as exc:
                logger.exception("[%s] [%s] Forecast error: %s", time_str, ticker, exc)

    # RSI reversal watcher — runs all session, throttled, pings on detection
    global _rsi_last_run
    if (RSI_WATCH_ENABLED and is_market_open() and
            (_rsi_last_run is None or (now - _rsi_last_run).total_seconds() >= RSI_WATCH_INTERVAL_SECONDS)):
        _rsi_last_run = now
        await run_rsi_watch(channel)

    if past_monitoring:
        return

    # Post "Monitoring started" at market open if bot was running before open
    if first_state and not first_state.monitoring_started_posted:
        market_open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
        monitoring_cutoff = now.replace(hour=MONITORING_END_HOUR, minute=MONITORING_END_MINUTE, second=0, microsecond=0)
        if market_open_time <= now < monitoring_cutoff:
            first_state.monitoring_started_posted = True
            if channel:
                await channel.send(
                    f"**Monitoring started for {today_str}** | "
                    f"{', '.join(active_tickers)} | {market_open_time.strftime('%I:%M %p')} - {monitoring_cutoff.strftime('%I:%M %p')} ET"
                )
                logger.info("[%s] Monitoring started message posted", time_str)
                await maybe_post_econ_briefing(channel, today_str)

    candle_ready_time = now.replace(hour=MARKET_OPEN_HOUR, minute=FIRST_CANDLE_READY_MINUTE, second=0, microsecond=0)
    past_first_candle = now >= candle_ready_time

    if not past_first_candle:
        logger.info("[%s] Waiting for first candle (ready at %s)", time_str, candle_ready_time.strftime("%I:%M %p"))
        return

    for ticker in list(active_tickers):
        st = states.get(ticker)
        if st is None:
            continue

        if st.first_candle_overextended:
            continue
        if st.signal_fired:
            continue

        logger.info("[%s] [%s] Poll cycle | first_candle=%s | bias=%s | broke_above=%s | broke_below=%s",
                    time_str, ticker,
                    f"${st.first_candle.high}/${st.first_candle.low}" if st.first_candle else "None",
                    st.daily_bias.direction if st.daily_bias else "None",
                    st.broke_above,
                    st.broke_below)

        try:
            # Step 0: Fetch pre-market levels (once)
            if st.premarket is None:
                pm = await get_premarket_levels(ticker)
                if pm:
                    st.premarket = pm
                    logger.info("[%s] [%s] Pre-market levels: high=$%s low=$%s vol=%s",
                                time_str, ticker, pm.high, pm.low, pm.volume)
                    await channel.send(embed=premarket_embed(pm))

            # Step 1: Get first candle range
            if st.first_candle is None:
                logger.info("[%s] [%s] Fetching first 5-min candle...", time_str, ticker)
                fc = await get_first_candle_range(client, ticker)
                if fc:
                    st.first_candle = fc
                    logger.info("[%s] [%s] First candle set: high=$%s low=$%s range=$%.2f type=%s",
                                time_str, ticker, fc.high, fc.low, fc.range_size, fc.candle_type)
                    await channel.send(embed=range_embed(fc))
                else:
                    logger.warning("[%s] [%s] First candle returned None — will retry next poll", time_str, ticker)

            # Step 1.5: Compute ATR
            if st.first_candle is not None and st.atr_value is None:
                logger.info("[%s] [%s] Computing ATR...", time_str, ticker)
                atr = await compute_atr(client, ticker)
                if atr is not None:
                    st.atr_value = atr
                    if st.first_candle.range_size > ATR_OVEREXTENDED_MULT * atr:
                        st.first_candle_overextended = True
                        logger.warning("[%s] [%s] Opening range OVEREXTENDED", time_str, ticker)
                        await channel.send(embed=overextended_embed(ticker, st.first_candle.range_size, atr))
                        continue
                    else:
                        logger.info("[%s] [%s] ATR check passed: $%.2f <= %.1fx $%.2f",
                                    time_str, ticker, st.first_candle.range_size, ATR_OVEREXTENDED_MULT, atr)

            # Step 2: Get daily bias + set direction from first candle
            if st.first_candle is not None and st.daily_bias is None:
                logger.info("[%s] [%s] Fetching daily bias...", time_str, ticker)
                bias = await get_daily_bias(client, ticker, today_open=st.first_candle.open_price)
                if bias:
                    set_bias_from_first_candle(bias, st.first_candle)
                    st.daily_bias = bias
                    logger.info("[%s] [%s] Daily bias set: %s (gap=%s %+.2f%% | fc=%s | rsi=%s)",
                                time_str, ticker, bias.direction, bias.gap_direction,
                                bias.gap_pct, st.first_candle.candle_type,
                                bias.rsi if bias.rsi else "N/A")
                    log_bias(bias, now)
                    await channel.send(embed=bias_embed(bias))
                else:
                    logger.warning("[%s] [%s] Daily bias returned None — will retry", time_str, ticker)

            # Step 3: Check for trade setups
            if st.daily_bias and st.first_candle and not st.signal_fired:
                result = await check_for_setup(st, client, ticker)
                if isinstance(result, TradeSignal):
                    filter_str = " | ".join(f"{f.name}:{f.reason}" for f in result.filters)
                    logger.info("[%s] [%s] SIGNAL FIRED: %s entry=$%s stop=$%s target=$%s | %s",
                                time_str, ticker, result.direction, result.entry_price,
                                result.stop_loss, result.target, filter_str)
                    log_filters(ticker, result.direction, result.filters, fired=True, now=now)
                    await channel.send("@everyone", embed=signal_embed(result), view=TradeSignalView(result))
                elif isinstance(result, FilteredSignal):
                    failed = ", ".join(f"{f.name}: {f.reason}" for f in result.failed_filters)
                    logger.info("[%s] [%s] Setup FILTERED: %s | Failed: %s | Cooldown %dm",
                                time_str, ticker, result.direction, failed, FILTER_COOLDOWN_MINUTES)
                    log_filters(ticker, result.direction, result.filters, fired=False, now=now)
                    await channel.send(embed=filtered_embed(result))
                else:
                    logger.debug("[%s] [%s] No setup detected", time_str, ticker)

        except Exception as exc:
            logger.exception("[%s] [%s] Monitor error: %s", time_str, ticker, exc)


@market_monitor.before_loop
async def before_monitor():
    await bot.wait_until_ready()


@market_monitor.error
async def monitor_error(exc):
    """An unhandled exception kills a tasks.loop permanently — log and restart it."""
    logger.exception("Market monitor loop crashed: %s — restarting in 30s", exc)
    await asyncio.sleep(30)
    market_monitor.restart()


async def run_bot():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env file")
        print("1. Create a bot at https://discord.com/developers/applications")
        print("2. Copy the token into your .env file")
        return

    async with bot:
        try:
            await bot.start(DISCORD_TOKEN)
        finally:
            await client.close()
