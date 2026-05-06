import asyncio
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pytz
import yfinance as yf

from market_data import MarketDataClient
from config import (
    SMA_PERIOD, RISK_REWARD_RATIO,
    RVOL_THRESHOLD, RVOL_LOOKBACK,
    ATR_PERIOD, ATR_OVEREXTENDED_MULT,
    EMA_FAST, EMA_SLOW,
    FILTER_COOLDOWN_MINUTES,
)

ET = pytz.timezone("US/Eastern")


@dataclass
class DailyBias:
    ticker: str
    direction: str
    current_price: float
    sma_value: float
    distance_pct: float

    @property
    def strength(self) -> str:
        abs_dist = abs(self.distance_pct)
        if abs_dist < 0.5:
            return "Weak"
        elif abs_dist < 1.5:
            return "Moderate"
        else:
            return "Strong"


@dataclass
class PremarketLevels:
    ticker: str
    high: float
    low: float
    volume: float


@dataclass
class FirstCandleRange:
    ticker: str
    high: float
    low: float
    open_price: float
    close_price: float

    @property
    def range_size(self) -> float:
        return self.high - self.low

    @property
    def candle_type(self) -> str:
        return "Bullish" if self.close_price > self.open_price else "Bearish"


@dataclass
class FilterStatus:
    name: str
    passed: bool
    reason: str


@dataclass
class TradeSignal:
    ticker: str
    direction: str
    entry_price: float
    stop_loss: float
    target: float
    risk: float
    reward: float
    timestamp: str
    filters: list[FilterStatus] = field(default_factory=list)


@dataclass
class FilteredSignal:
    ticker: str
    direction: str
    entry_price: float
    timestamp: str
    filters: list[FilterStatus] = field(default_factory=list)

    @property
    def failed_filters(self) -> list[FilterStatus]:
        return [f for f in self.filters if not f.passed]

    @property
    def passed_filters(self) -> list[FilterStatus]:
        return [f for f in self.filters if f.passed]


@dataclass
class TradeOutcome:
    ticker: str
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    target: float
    result: str  # "TARGET_HIT", "STOPPED_OUT", "EXPIRED"
    pnl: float
    timestamp: str


class StrategyState:
    """Tracks the live state of the first-candle strategy throughout the session."""

    def __init__(self):
        self.trading_day: str | None = None
        self.daily_bias: DailyBias | None = None
        self.first_candle: FirstCandleRange | None = None
        self.premarket: PremarketLevels | None = None
        self.broke_above: bool = False
        self.broke_below: bool = False
        self.signal_fired: bool = False
        self.monitoring_ended: bool = False
        self.atr_value: float | None = None
        self.first_candle_overextended: bool = False
        self.active_trade: TradeSignal | None = None
        self.trade_outcome: TradeOutcome | None = None
        self.filter_cooldown_until: datetime | None = None
        self.filtered_signals: list[FilteredSignal] = []

    def reset(self, today: str | None = None):
        self.__init__()
        self.trading_day = today


# ── Core strategy functions ─────────────────────────────────

async def get_daily_bias(client: MarketDataClient, ticker: str) -> DailyBias | None:
    daily = await client.fetch_candles(ticker, interval="D", count=SMA_PERIOD + 10)
    if daily.empty or len(daily) < SMA_PERIOD:
        return None

    daily["SMA"] = daily["close"].rolling(window=SMA_PERIOD).mean()
    latest = daily.iloc[-1]
    sma_val = float(latest["SMA"])
    close_val = float(latest["close"])
    distance_pct = ((close_val - sma_val) / sma_val) * 100
    direction = "BULLISH" if close_val > sma_val else "BEARISH"

    return DailyBias(
        ticker=ticker,
        direction=direction,
        current_price=round(close_val, 2),
        sma_value=round(sma_val, 2),
        distance_pct=round(distance_pct, 2),
    )


async def get_first_candle_range(client: MarketDataClient, ticker: str) -> FirstCandleRange | None:
    data = await client.fetch_candles(ticker, interval="5", count=50)
    if data.empty:
        return None

    today = datetime.now(ET).date()
    for _, row in data.iterrows():
        ts = pd.to_datetime(row["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC").astimezone(ET)
        else:
            ts = ts.astimezone(ET)
        if ts.date() == today and ts.hour == 9 and ts.minute == 30:
            return FirstCandleRange(
                ticker=ticker,
                high=round(float(row["high"]), 2),
                low=round(float(row["low"]), 2),
                open_price=round(float(row["open"]), 2),
                close_price=round(float(row["close"]), 2),
            )
    return None


async def get_premarket_levels(ticker: str) -> PremarketLevels | None:
    """Fetch pre-market high/low using yfinance extended hours data."""
    def _fetch():
        t = yf.Ticker(ticker)
        df = t.history(period="1d", interval="1m", prepost=True)
        if df is None or df.empty:
            return None
        return df

    try:
        df = await asyncio.to_thread(_fetch)
    except Exception:
        return None

    if df is None or df.empty:
        return None

    today = datetime.now(ET).date()
    pre_data = []
    for idx in df.index:
        ts = idx.tz_convert(ET) if idx.tzinfo else idx.tz_localize(ET)
        if ts.date() == today and ts.hour < 9:
            pre_data.append(df.loc[idx])
        elif ts.date() == today and ts.hour == 9 and ts.minute < 30:
            pre_data.append(df.loc[idx])

    if not pre_data:
        return None

    pre_df = pd.DataFrame(pre_data)
    return PremarketLevels(
        ticker=ticker,
        high=round(float(pre_df["High"].max()), 2),
        low=round(float(pre_df["Low"].min()), 2),
        volume=round(float(pre_df["Volume"].sum()), 0),
    )


# ── Indicator functions ─────────────────────────────────────

async def compute_atr(client: MarketDataClient, ticker: str, period: int = ATR_PERIOD) -> float | None:
    data = await client.fetch_candles(ticker, interval="5", count=period + 20)
    if data.empty or len(data) < period + 1:
        return None

    highs = data["high"].astype(float)
    lows = data["low"].astype(float)
    closes = data["close"].astype(float)

    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean().iloc[-1]
    return round(float(atr), 4) if not np.isnan(atr) else None


def compute_vwap(candles: pd.DataFrame) -> float | None:
    if candles.empty:
        return None

    typical_price = (candles["high"].astype(float) + candles["low"].astype(float) + candles["close"].astype(float)) / 3
    volume = candles["volume"].astype(float)

    cum_tpv = (typical_price * volume).cumsum()
    cum_vol = volume.cumsum()

    if cum_vol.iloc[-1] == 0:
        return None

    vwap = cum_tpv.iloc[-1] / cum_vol.iloc[-1]
    return round(float(vwap), 2)


def check_relative_volume(candles: pd.DataFrame, lookback: int = RVOL_LOOKBACK) -> tuple[bool, float]:
    if len(candles) < lookback + 1:
        return True, 0.0

    volumes = candles["volume"].astype(float)
    avg_vol = volumes.iloc[-(lookback + 1):-1].mean()
    if avg_vol == 0:
        return True, 0.0

    latest_vol = volumes.iloc[-1]
    ratio = round(float(latest_vol / avg_vol), 2)
    return ratio >= RVOL_THRESHOLD, ratio


def check_ema_crossover(candles_5m: pd.DataFrame) -> tuple[str, float, float]:
    closes = candles_5m["close"].astype(float)
    ema_fast = closes.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = closes.ewm(span=EMA_SLOW, adjust=False).mean()

    fast_val = round(float(ema_fast.iloc[-1]), 2)
    slow_val = round(float(ema_slow.iloc[-1]), 2)
    direction = "BULLISH" if fast_val > slow_val else "BEARISH"
    return direction, fast_val, slow_val


def check_15m_trend(candles_15m: pd.DataFrame) -> tuple[str, float, float] | None:
    """Check trend on 15-min timeframe using 9/21 EMA."""
    if candles_15m.empty or len(candles_15m) < EMA_SLOW:
        return None
    closes = candles_15m["close"].astype(float)
    ema_fast = closes.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = closes.ewm(span=EMA_SLOW, adjust=False).mean()

    fast_val = round(float(ema_fast.iloc[-1]), 2)
    slow_val = round(float(ema_slow.iloc[-1]), 2)
    direction = "BULLISH" if fast_val > slow_val else "BEARISH"
    return direction, fast_val, slow_val


def _apply_filters(
    state: StrategyState,
    direction: str,
    entry_price: float,
    candles_1m: pd.DataFrame,
    candles_5m: pd.DataFrame | None,
    candles_15m: pd.DataFrame | None = None,
) -> list[FilterStatus]:
    filters = []

    # ATR filter
    if state.atr_value is not None and state.first_candle is not None:
        overextended = state.first_candle_overextended
        filters.append(FilterStatus(
            name="ATR",
            passed=not overextended,
            reason=f"Range ${state.first_candle.range_size:.2f} {'>' if overextended else '<='} {ATR_OVEREXTENDED_MULT}x ATR ${state.atr_value:.2f}"
        ))

    # VWAP filter
    today = datetime.now(ET).date()
    today_candles = candles_1m.copy()
    timestamps = pd.to_datetime(today_candles["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC").dt.tz_convert(ET)
    else:
        timestamps = timestamps.dt.tz_convert(ET)
    today_candles = today_candles[timestamps.dt.date == today]

    vwap = compute_vwap(today_candles)
    if vwap is not None:
        if direction == "LONG":
            passed = entry_price > vwap
            filters.append(FilterStatus(name="VWAP", passed=passed,
                reason=f"Entry ${entry_price} {'>' if passed else '<='} VWAP ${vwap}"))
        else:
            passed = entry_price < vwap
            filters.append(FilterStatus(name="VWAP", passed=passed,
                reason=f"Entry ${entry_price} {'<' if passed else '>='} VWAP ${vwap}"))

    # Relative Volume filter
    rvol_passed, rvol_ratio = check_relative_volume(candles_1m)
    filters.append(FilterStatus(name="RVOL", passed=rvol_passed,
        reason=f"{rvol_ratio}x avg volume ({'>' if rvol_passed else '<'} {RVOL_THRESHOLD}x threshold)"))

    # EMA crossover filter (5-min)
    if candles_5m is not None and len(candles_5m) >= EMA_SLOW:
        ema_dir, ema_fast, ema_slow = check_ema_crossover(candles_5m)
        if direction == "LONG":
            passed = ema_dir == "BULLISH"
        else:
            passed = ema_dir == "BEARISH"
        filters.append(FilterStatus(name="EMA 5m", passed=passed,
            reason=f"9 EMA ${ema_fast} {'>' if ema_dir == 'BULLISH' else '<'} 21 EMA ${ema_slow} ({ema_dir})"))

    # 15-min trend confirmation
    if candles_15m is not None and len(candles_15m) >= EMA_SLOW:
        result_15m = check_15m_trend(candles_15m)
        if result_15m is not None:
            trend_dir, f15, s15 = result_15m
            if direction == "LONG":
                passed = trend_dir == "BULLISH"
            else:
                passed = trend_dir == "BEARISH"
            filters.append(FilterStatus(name="EMA 15m", passed=passed,
                reason=f"9 EMA ${f15} {'>' if trend_dir == 'BULLISH' else '<'} 21 EMA ${s15} ({trend_dir})"))

    # Pre-market level confluence
    if state.premarket is not None:
        pm = state.premarket
        if direction == "LONG" and entry_price > pm.high:
            filters.append(FilterStatus(name="PM Level", passed=True,
                reason=f"Entry ${entry_price} > PM High ${pm.high} (breakout confluence)"))
        elif direction == "SHORT" and entry_price < pm.low:
            filters.append(FilterStatus(name="PM Level", passed=True,
                reason=f"Entry ${entry_price} < PM Low ${pm.low} (breakdown confluence)"))
        else:
            filters.append(FilterStatus(name="PM Level", passed=True,
                reason=f"PM Range ${pm.low}-${pm.high} (no confluence, neutral)"))

    return filters


# ── Trade outcome tracking ──────────────────────────────────

async def check_trade_outcome(state: StrategyState, client: MarketDataClient, ticker: str) -> TradeOutcome | None:
    """Check if an active trade has hit its target or stop."""
    if state.active_trade is None:
        return None

    trade = state.active_trade
    data = await client.fetch_candles(ticker, interval="1", count=5)
    if data.empty:
        return None

    for _, row in data.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        if trade.direction == "LONG":
            if low <= trade.stop_loss:
                return TradeOutcome(
                    ticker=ticker, direction="LONG",
                    entry_price=trade.entry_price, exit_price=trade.stop_loss,
                    stop_loss=trade.stop_loss, target=trade.target,
                    result="STOPPED_OUT", pnl=round(trade.stop_loss - trade.entry_price, 2),
                    timestamp=datetime.now(ET).strftime("%I:%M %p ET"),
                )
            if high >= trade.target:
                return TradeOutcome(
                    ticker=ticker, direction="LONG",
                    entry_price=trade.entry_price, exit_price=trade.target,
                    stop_loss=trade.stop_loss, target=trade.target,
                    result="TARGET_HIT", pnl=round(trade.target - trade.entry_price, 2),
                    timestamp=datetime.now(ET).strftime("%I:%M %p ET"),
                )
        else:
            if high >= trade.stop_loss:
                return TradeOutcome(
                    ticker=ticker, direction="SHORT",
                    entry_price=trade.entry_price, exit_price=trade.stop_loss,
                    stop_loss=trade.stop_loss, target=trade.target,
                    result="STOPPED_OUT", pnl=round(trade.entry_price - trade.stop_loss, 2),
                    timestamp=datetime.now(ET).strftime("%I:%M %p ET"),
                )
            if low <= trade.target:
                return TradeOutcome(
                    ticker=ticker, direction="SHORT",
                    entry_price=trade.entry_price, exit_price=trade.target,
                    stop_loss=trade.stop_loss, target=trade.target,
                    result="TARGET_HIT", pnl=round(trade.entry_price - trade.target, 2),
                    timestamp=datetime.now(ET).strftime("%I:%M %p ET"),
                )

    return None


# ── Setup detection ─────────────────────────────────────────

async def check_for_setup(state: StrategyState, client: MarketDataClient, ticker: str) -> TradeSignal | FilteredSignal | None:
    if state.daily_bias is None or state.first_candle is None:
        return None
    if state.signal_fired:
        return None
    if state.first_candle_overextended:
        return None

    # Check cooldown from previous filtered signal
    if state.filter_cooldown_until is not None:
        if datetime.now(ET) < state.filter_cooldown_until:
            return None
        state.filter_cooldown_until = None

    data = await client.fetch_candles(ticker, interval="1", count=200)
    if data.empty or len(data) < 3:
        return None

    candles = data.tail(200)
    box_high = state.first_candle.high
    box_low = state.first_candle.low
    bias = state.daily_bias.direction

    for i in range(len(candles)):
        row = candles.iloc[i]
        close = float(row["close"])

        if close > box_high and not state.broke_above:
            state.broke_above = True
        if close < box_low and not state.broke_below:
            state.broke_below = True

    if len(candles) < 3:
        return None

    recent = candles.iloc[-1]
    prev = candles.iloc[-2]
    recent_close = float(recent["close"])
    recent_open = float(recent["open"])
    prev_high = float(prev["high"])

    now_str = datetime.now(ET).strftime("%I:%M %p ET")

    if state.broke_below and bias == "BEARISH" and not state.signal_fired:
        if prev_high >= box_low * 0.999:
            if recent_close < recent_open and recent_close < box_low:
                stop_loss = box_high
                risk = stop_loss - recent_close
                if risk > 0:
                    return await _build_signal_or_filtered(
                        state, client, ticker, "SHORT",
                        recent_close, stop_loss, risk, now_str, candles,
                    )

    if state.broke_above and bias == "BULLISH" and not state.signal_fired:
        prev_low = float(prev["low"])
        if prev_low <= box_high * 1.001:
            if recent_close > recent_open and recent_close > box_high:
                stop_loss = box_low
                risk = recent_close - stop_loss
                if risk > 0:
                    return await _build_signal_or_filtered(
                        state, client, ticker, "LONG",
                        recent_close, stop_loss, risk, now_str, candles,
                    )

    return None


async def _build_signal_or_filtered(
    state: StrategyState,
    client: MarketDataClient,
    ticker: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    risk: float,
    timestamp: str,
    candles_1m: pd.DataFrame,
) -> TradeSignal | FilteredSignal:
    candles_5m = None
    candles_15m = None
    try:
        candles_5m = await client.fetch_candles(ticker, interval="5", count=100)
    except Exception:
        pass
    try:
        candles_15m = await client.fetch_candles(ticker, interval="15", count=100)
    except Exception:
        pass

    filters = _apply_filters(state, direction, entry_price, candles_1m, candles_5m, candles_15m)
    all_passed = all(f.passed for f in filters)

    if all_passed:
        target = entry_price - (risk * RISK_REWARD_RATIO) if direction == "SHORT" else entry_price + (risk * RISK_REWARD_RATIO)
        state.signal_fired = True
        signal = TradeSignal(
            ticker=ticker, direction=direction,
            entry_price=round(entry_price, 2), stop_loss=round(stop_loss, 2),
            target=round(target, 2), risk=round(risk, 2),
            reward=round(risk * RISK_REWARD_RATIO, 2),
            timestamp=timestamp, filters=filters,
        )
        state.active_trade = signal
        return signal
    else:
        fs = FilteredSignal(
            ticker=ticker, direction=direction,
            entry_price=round(entry_price, 2),
            timestamp=timestamp, filters=filters,
        )
        state.filtered_signals.append(fs)
        state.filter_cooldown_until = datetime.now(ET) + timedelta(minutes=FILTER_COOLDOWN_MINUTES)
        return fs
