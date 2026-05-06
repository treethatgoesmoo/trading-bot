import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
import pytz

from market_data import MarketDataClient
from config import (
    SMA_PERIOD, RISK_REWARD_RATIO,
    RVOL_THRESHOLD, RVOL_LOOKBACK,
    ATR_PERIOD, ATR_OVEREXTENDED_MULT,
    EMA_FAST, EMA_SLOW,
)

ET = pytz.timezone("US/Eastern")


@dataclass
class DailyBias:
    ticker: str
    direction: str  # "BULLISH" or "BEARISH"
    current_price: float
    sma_value: float
    distance_pct: float  # % distance from SMA

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
    direction: str  # "LONG" or "SHORT"
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


class StrategyState:
    """Tracks the live state of the first-candle strategy throughout the session."""

    def __init__(self):
        self.trading_day: str | None = None
        self.daily_bias: DailyBias | None = None
        self.first_candle: FirstCandleRange | None = None
        self.broke_above: bool = False
        self.broke_below: bool = False
        self.signal_fired: bool = False
        self.monitoring_ended: bool = False
        self.atr_value: float | None = None
        self.first_candle_overextended: bool = False

    def reset(self, today: str | None = None):
        self.__init__()
        self.trading_day = today


# ── Core strategy functions ─────────────────────────────────

async def get_daily_bias(client: MarketDataClient, ticker: str) -> DailyBias | None:
    """Determine bullish/bearish bias from daily 50 SMA."""
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
    """Get the first 5-minute candle range for today."""
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


# ── Indicator functions ─────────────────────────────────────

async def compute_atr(client: MarketDataClient, ticker: str, period: int = ATR_PERIOD) -> float | None:
    """Compute ATR from 5-min candles."""
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
    """Compute VWAP from intraday candles. Expects today's candles only."""
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
    """Check if the latest candle's volume exceeds threshold * average of prior candles."""
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
    """Check 9/21 EMA crossover direction on 5-min candles."""
    closes = candles_5m["close"].astype(float)
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
) -> list[FilterStatus]:
    """Evaluate all indicator filters. Returns list of FilterStatus."""
    filters = []

    # ATR filter (already computed and stored on state)
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
            filters.append(FilterStatus(
                name="VWAP",
                passed=passed,
                reason=f"Entry ${entry_price} {'>' if passed else '<='} VWAP ${vwap}"
            ))
        else:
            passed = entry_price < vwap
            filters.append(FilterStatus(
                name="VWAP",
                passed=passed,
                reason=f"Entry ${entry_price} {'<' if passed else '>='} VWAP ${vwap}"
            ))

    # Relative Volume filter
    rvol_passed, rvol_ratio = check_relative_volume(candles_1m)
    filters.append(FilterStatus(
        name="RVOL",
        passed=rvol_passed,
        reason=f"{rvol_ratio}x avg volume ({'>' if rvol_passed else '<'} {RVOL_THRESHOLD}x threshold)"
    ))

    # EMA crossover filter
    if candles_5m is not None and len(candles_5m) >= EMA_SLOW:
        ema_dir, ema_fast, ema_slow = check_ema_crossover(candles_5m)
        if direction == "LONG":
            passed = ema_dir == "BULLISH"
        else:
            passed = ema_dir == "BEARISH"
        filters.append(FilterStatus(
            name="EMA",
            passed=passed,
            reason=f"9 EMA ${ema_fast} {'>' if ema_dir == 'BULLISH' else '<'} 21 EMA ${ema_slow} ({ema_dir})"
        ))

    return filters


# ── Setup detection ─────────────────────────────────────────

async def check_for_setup(state: StrategyState, client: MarketDataClient, ticker: str) -> TradeSignal | FilteredSignal | None:
    """Poll the latest 1-min candles and check for a break-and-retest setup."""
    if state.daily_bias is None or state.first_candle is None:
        return None
    if state.signal_fired:
        return None
    if state.first_candle_overextended:
        return None

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

    # SHORT setup: broke below + bearish bias + retest near box_low + bearish candle
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

    # LONG setup: broke above + bullish bias + retest near box_high + bullish candle
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
    """Apply indicator filters. Return TradeSignal if all pass, FilteredSignal otherwise."""
    candles_5m = None
    try:
        candles_5m = await client.fetch_candles(ticker, interval="5", count=100)
    except Exception:
        pass

    filters = _apply_filters(state, direction, entry_price, candles_1m, candles_5m)
    all_passed = all(f.passed for f in filters)

    if all_passed:
        target = entry_price - (risk * RISK_REWARD_RATIO) if direction == "SHORT" else entry_price + (risk * RISK_REWARD_RATIO)
        state.signal_fired = True
        return TradeSignal(
            ticker=ticker,
            direction=direction,
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            target=round(target, 2),
            risk=round(risk, 2),
            reward=round(risk * RISK_REWARD_RATIO, 2),
            timestamp=timestamp,
            filters=filters,
        )
    else:
        return FilteredSignal(
            ticker=ticker,
            direction=direction,
            entry_price=round(entry_price, 2),
            timestamp=timestamp,
            filters=filters,
        )
