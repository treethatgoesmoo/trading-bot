"""Discord embeds for the 0DTE (first candle) feature — pure builders, no I/O."""

from datetime import datetime

import discord
import pytz

from config import (
    ATR_OVEREXTENDED_MULT,
    FILTER_COOLDOWN_MINUTES,
    FORECAST_INTERVAL_MINUTES,
)
from zero_dte.strategy import (
    DailyBias,
    DirectionForecast,
    FilteredSignal,
    FirstCandleRange,
    PremarketLevels,
    TradeOutcome,
    TradeSignal,
)

ET = pytz.timezone("US/Eastern")


def bias_embed(bias: DailyBias) -> discord.Embed:
    if bias.direction == "BULLISH":
        color, emoji = 0x00CC66, "\U0001f7e2"
    elif bias.direction == "BEARISH":
        color, emoji = 0xFF4444, "\U0001f534"
    else:  # NEUTRAL — signals conflict, no entries today
        color, emoji = 0xFFAA00, "\U0001f7e1"
    trend_emoji = "\U0001f7e2" if bias.trend == "BULLISH" else "\U0001f534"
    gap_emoji = "\U0001f7e2" if bias.gap_direction == "GAP_UP" else ("\U0001f534" if bias.gap_direction == "GAP_DOWN" else "\U0001f7e1")
    md_emoji = "\U0001f7e2" if bias.multi_day == "BULLISH" else ("\U0001f534" if bias.multi_day == "BEARISH" else "\U0001f7e1")

    embed = discord.Embed(title=f"{emoji} Daily Bias: {bias.direction}", color=color)
    if bias.direction == "NEUTRAL":
        embed.description = ("First candle, gap and 50 SMA trend disagree — no majority. "
                             "No break-and-retest entries today.")
    embed.add_field(name="Ticker", value=bias.ticker, inline=True)
    embed.add_field(name="Gap", value=f"{gap_emoji} {bias.gap_direction} ({bias.gap_pct:+.2f}%)", inline=True)
    embed.add_field(name="Prev Day Range", value=f"${bias.prev_low} - ${bias.prev_high}", inline=True)
    embed.add_field(name="5-Day Trend", value=f"{md_emoji} {bias.multi_day} ({bias.green_days}G / {bias.red_days}R)", inline=True)
    embed.add_field(name="50 SMA", value=f"{trend_emoji} {bias.trend} (${bias.current_price} vs ${bias.sma_value})", inline=True)
    rsi_str = f"{bias.rsi} ({bias.rsi_signal})" if bias.rsi is not None else "N/A"
    embed.add_field(name="Daily RSI", value=rsi_str, inline=True)
    embed.set_footer(text="Informational only — entries fire either direction (graded 50.9% vs "
                          "52.6% always-BULLISH over 60d; NEUTRAL still stands down)")
    return embed


def forecast_embed(fc: DirectionForecast) -> discord.Embed:
    if fc.direction == "BULLISH":
        color, emoji = 0x00CC66, "\U0001f7e2"
    elif fc.direction == "BEARISH":
        color, emoji = 0xFF4444, "\U0001f534"
    else:  # NEUTRAL / VOLATILE
        color, emoji = 0xFFAA00, "\U0001f7e1"

    title_dir = "VOLATILE ⚠️" if fc.direction == "VOLATILE" else fc.direction
    embed = discord.Embed(
        title=f"{emoji} {fc.interval}-Min Forecast: {title_dir} {fc.ticker}",
        color=color,
    )
    if fc.direction == "VOLATILE":
        embed.description = ("Indicators are contradicting each other — expect chop. "
                             "Low-conviction read; caution on entries.")
        if fc.lean:
            lean_icon = "\U0001f7e2" if fc.lean == "BULLISH" else "\U0001f534"
            embed.add_field(name="Lean", value=f"{lean_icon} {fc.lean}", inline=True)
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
        tf = f" ({fc.divergence_tf})" if fc.divergence_tf else ""
        embed.add_field(name="Divergence", value=f"⚠️ {div_str}{tf}", inline=True)
    if fc.rsi_caution:
        embed.add_field(name="RSI Watch", value=f"⚠️ {fc.rsi_caution}", inline=False)

    # Day context: the ACTUAL session range so far (replaced the projected
    # day forecast — tracked fact, not an estimate).
    if fc.day_high is not None and fc.day_low is not None:
        embed.add_field(
            name="Today so far",
            value=f"High ${fc.day_high} · Low ${fc.day_low}",
            inline=False,
        )

    # Signal breakdown
    sig_lines = []
    for name, val in fc.signals.items():
        w = fc.weights.get(name, 0)
        icon = "\U0001f7e2" if val == "BULLISH" else ("\U0001f534" if val == "BEARISH" else "\U0001f7e1")
        sig_lines.append(f"{icon} {name} (w:{w})")
    if sig_lines:
        embed.add_field(name="Signals", value=" | ".join(sig_lines), inline=False)

    embed.set_footer(text=f"Forecast at {fc.timestamp} | Next {fc.interval} min outlook")
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
    # With both 15m and 5m streams graded, break the headline number down.
    intervals = sorted({r.get("interval", FORECAST_INTERVAL_MINUTES) for r in results})
    if len(intervals) > 1:
        for iv in intervals:
            iv_scored = [r for r in scored if r.get("interval", FORECAST_INTERVAL_MINUTES) == iv]
            iv_correct = sum(1 for r in iv_scored if r["correct"])
            iv_pct = iv_correct / len(iv_scored) * 100 if iv_scored else 0
            embed.add_field(name=f"{iv}m", value=f"{iv_correct}/{len(iv_scored)} ({iv_pct:.0f}%)", inline=True)
    if user_score and user_score[1] > 0:
        u_correct, u_total = user_score
        embed.add_field(name="Your Calls", value=f"{u_correct}/{u_total} ({u_correct / u_total * 100:.0f}%)", inline=True)

    # The line list sticks to the main interval — a 5m stream would drown it
    # (full breakdown lives behind View Details).
    lines = []
    for r in results:
        if len(intervals) > 1 and r.get("interval", FORECAST_INTERVAL_MINUTES) != max(intervals):
            continue
        ftime = datetime.fromisoformat(r["time"]).astimezone(ET).strftime("%I:%M %p")
        if r.get("correct") is None:
            icon = "\U0001f7e1"
        elif r["correct"]:
            icon = "✅"
        else:
            icon = "❌"
        lines.append(f"{icon} {ftime}: {r['direction']} ({r['confidence']}%) -> {r['actual']} ({r['move']:+.2f})")
    if lines:
        name = f"Forecasts ({max(intervals)}m)" if len(intervals) > 1 else "Forecasts"
        embed.add_field(name=name, value="\n".join(lines[:10]), inline=False)

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


def signal_embed(signal: TradeSignal,
                 record: tuple[int, int, float] | None = None,
                 session: tuple[float, float] | None = None) -> discord.Embed:
    """Trade alert embed. `record` is (wins, total, total_r) of resolved live
    trades in this direction — shown so a historically weak side (e.g. LONG)
    is visible at decision time instead of only in !stats after the fact.
    `session` is (high, low) of the session so far."""
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
    if signal.retest_level:
        back = "back down to" if signal.direction == "LONG" else "back up to"
        embed.add_field(
            name="⏱️ Entry plan",
            value=(
                f"No need to chase ${signal.entry_price}. A wiggle {back} the broken level "
                f"**${signal.retest_level}** is routine after a break-and-retest and usually a better "
                f"fill — it does NOT void the setup; only the stop (${signal.stop_loss}) does.\n"
                f"Options: limit order at the mid — consider half now, half on a pullback toward "
                f"${signal.retest_level}."
            ),
            inline=False)
    if session is not None:
        embed.add_field(name="Today so far", value=f"High ${session[0]} · Low ${session[1]}", inline=False)
    if signal.filters:
        filter_lines = [f"✅ {f.name}: {f.reason}" for f in signal.filters]
        embed.add_field(name="Filters", value="\n".join(filter_lines), inline=False)
    if record is not None:
        wins, total, total_r = record
        if total > 0:
            pct = wins / total * 100
            line = f"{signal.direction} signals so far: {wins}/{total} wins ({pct:.0f}%), {total_r:+.1f}R"
            if total >= 3 and pct < 34:
                line += "\n⚠️ This side has been below breakeven (needs >33% at 2R) — size accordingly."
            embed.add_field(name="📊 Track record", value=line, inline=False)
    embed.set_footer(text=f"Signal at {signal.timestamp} | First Candle Strategy")
    return embed


def filtered_embed(fs: FilteredSignal,
                   session: tuple[float, float] | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ Setup Filtered: {fs.direction} {fs.ticker}",
        color=0xFFA500,
    )
    embed.add_field(name="Entry (would be)", value=f"${fs.entry_price}", inline=True)
    embed.add_field(name="Time", value=fs.timestamp, inline=True)
    embed.add_field(name="Cooldown", value=f"{FILTER_COOLDOWN_MINUTES} min", inline=True)
    if session is not None:
        embed.add_field(name="Today so far", value=f"High ${session[0]} · Low ${session[1]}", inline=False)
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


def daily_summary_embed(today_str: str, active_tickers: list[str], states: dict) -> discord.Embed:
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
            lines.append(f"Morning bias: {st.daily_bias.direction} ({st.daily_bias.strength})")
        if st.last_forecast:
            fc = st.last_forecast
            fc_icon = "\U0001f7e2" if fc.direction == "BULLISH" else ("\U0001f534" if fc.direction == "BEARISH" else "\U0001f7e1")
            dir_txt = f"VOLATILE, leaning {fc.lean}" if fc.direction == "VOLATILE" and fc.lean else fc.direction
            lines.append(f"Intraday now: {fc_icon} {dir_txt} ({fc.confidence}%, {fc.timestamp})")
        if st.first_candle:
            lines.append(f"Range: ${st.first_candle.high} / ${st.first_candle.low} (${st.first_candle.range_size:.2f})")
        if st.session_high is not None and st.session_low is not None:
            lines.append(f"Day H/L: ${st.session_high} / ${st.session_low}")
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

    embed.set_footer(text=f"Morning bias (~9:35 ET) drove today's entries; 'Intraday now' is the latest "
                          f"{FORECAST_INTERVAL_MINUTES}-min forecast and can diverge as the tape changes")
    return embed


def bb_alert_embed(alert) -> discord.Embed:
    """A Bollinger band touch ping (1m/5m upper or lower band)."""
    from zero_dte.bb_watcher import BB_TF_LABEL
    if alert.band == "upper":
        color, arrow, side = 0xFF4444, "⬆️", "Upper"   # stretched high → mean-reversion risk down
    else:
        color, arrow, side = 0x00CC66, "⬇️", "Lower"
    tf = BB_TF_LABEL[alert.timeframe]
    embed = discord.Embed(
        title=f"\U0001f3af {arrow} Bollinger {side} Band — {alert.ticker} ({tf})",
        color=color,
    )
    embed.add_field(name="Signal", value=alert.message, inline=False)
    embed.add_field(name="Close", value=f"${alert.price}", inline=True)
    embed.add_field(name=f"{side} band", value=f"${alert.band_value}", inline=True)
    embed.add_field(name="Mid (SMA)", value=f"${alert.middle}", inline=True)
    embed.set_footer(text=f"Band touch at {alert.bar_time} — stretched, not a direction call; confirm with price action")
    return embed


def candle_pattern_embed(ticker: str, timeframe: str, alerts: list) -> discord.Embed:
    """Named candlestick pattern(s) on the last completed 1h/1D candle.

    One embed per (ticker, timeframe, candle) — a candle matching several
    patterns posts once, listing every match.
    """
    from zero_dte.candle_watcher import CANDLE_TF_LABEL
    directions = {a.direction for a in alerts}
    if directions == {"BULLISH"}:
        color, arrow = 0x00CC66, "⬆️"
    elif directions == {"BEARISH"}:
        color, arrow = 0xFF4444, "⬇️"
    else:
        color, arrow = 0x999999, "➖"   # indecision or conflicting matches
    tf = CANDLE_TF_LABEL[timeframe]
    first = alerts[0]
    embed = discord.Embed(
        title=f"\U0001f56f️ {arrow} Candlestick pattern — {ticker} ({tf})",
        color=color,
    )
    for a in alerts:
        side = a.direction.title() if a.direction else "Indecision"
        embed.add_field(name=f"{a.name} ({side})",
                        value=f"{a.candles}-candle pattern, close ${a.close:.2f}",
                        inline=False)
    embed.set_footer(text=f"Candle {first.bar_time} — observational log; daily/weekly patterns "
                          f"backtested to no directional edge (strategy log), 1h untested")
    return embed


def vix_alert_embed(alert) -> discord.Embed:
    """A VIX regime-cross or spike ping (risk-off / calming)."""
    if alert.direction == "BULLISH":
        color, arrow = 0x00CC66, "⬆️"   # SPY tailwind
    else:
        color, arrow = 0xFF4444, "⬇️"   # SPY headwind
    label = "Spike" if alert.kind == "SPIKE" else "Regime"
    embed = discord.Embed(title=f"\U0001f4c9 VIX {label} — {alert.level:.1f}", color=color)
    embed.add_field(name="Signal", value=alert.message, inline=False)
    embed.add_field(name="VIX", value=f"{alert.level:.1f}", inline=True)
    if alert.change_pct:
        embed.add_field(name="Move", value=f"{alert.change_pct:+.1f}%", inline=True)
    embed.add_field(name="SPY implication", value=f"{arrow} {alert.direction}", inline=True)
    embed.set_footer(text="VIX is inverse to SPY — confirm with price action")
    return embed


