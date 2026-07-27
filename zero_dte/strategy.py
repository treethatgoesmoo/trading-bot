import asyncio
import json
import logging
import os
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import pytz
import yfinance as yf

from core.market_data import MarketDataClient
from core.indicators import rsi_series, pivots as _pivots, atr as _atr_series
from core.paths import INDICATOR_WEIGHTS_FILE as _WEIGHTS_FILE
from config import (
    SMA_PERIOD, RISK_REWARD_RATIO,
    RVOL_THRESHOLD, RVOL_LOOKBACK,
    ATR_PERIOD, ATR_OVEREXTENDED_MULT,
    EMA_FAST, EMA_SLOW,
    FILTER_COOLDOWN_MINUTES,
    FORECAST_CONFLICT_RATIO,
    FORECAST_INTERVAL_MINUTES,
    FORECAST_VOLATILE_CONFIDENCE,
    RSI_DIV_LOOKBACK, RSI_PIVOT_ORDER,
    RSI_DIV_RSI_GATE,
    RSI_OB_WARN, RSI_OS_WARN,
    TUNED_WEIGHT_FLOOR,
    VIX_FORECAST_WEIGHT,
)
from core import vix

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# Default 30-min forecast indicator weights (from the original backtest:
# RSI 55.2%, EMA 54.7%, VWAP 53%, MACD 52.7%). Overridable live via
# data/indicator_weights.json — see tune_weights.py.
INDICATOR_WEIGHTS = {
    "RSI": 3.0,
    "EMA": 2.5,
    "VWAP": 2.0,
    "MACD": 2.0,
    "MACD_Mom": 0.5,
    # Modest default: the detector switched to pivot-based divergence (see
    # _detect_divergence), so the old endpoint-compare backtest no longer
    # justifies a top-tier weight. Retune from live results with tune_weights.py.
    "Divergence": 1.5,
    "Volume": 1.0,
    "Momentum": 1.0,
    "VIX": VIX_FORECAST_WEIGHT,
}

_weights_cache: tuple[float, dict] | None = None


def _weight_overrides() -> dict:
    """Tuned per-indicator weights from data/indicator_weights.json, if present.

    Written by tune_weights.py --write; reloaded automatically on file change.
    Clamped to TUNED_WEIGHT_FLOOR: a 0-weight indicator still votes but drops
    out of the confidence denominator and the VOLATILE conflict guard, so a
    shrunken ensemble could print 100% confidence over a zeroed dissenter.
    """
    global _weights_cache
    try:
        mtime = os.path.getmtime(_WEIGHTS_FILE)
    except OSError:
        return {}
    if _weights_cache is None or _weights_cache[0] != mtime:
        try:
            with open(_WEIGHTS_FILE) as f:
                data = json.load(f)
            overrides = {k: max(float(v), TUNED_WEIGHT_FLOOR)
                         for k, v in data.get("weights", data).items()}
            _weights_cache = (mtime, overrides)
            logger.info("Loaded indicator weight overrides: %s", overrides)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", _WEIGHTS_FILE, exc)
            return {}
    return _weights_cache[1]


@dataclass
class DailyBias:
    ticker: str
    direction: str  # trading bias set from first candle + gap
    trend: str  # long-term 50 SMA
    gap_direction: str  # "GAP_UP", "GAP_DOWN", or "FLAT"
    gap_pct: float
    multi_day: str  # 5-day trend
    current_price: float
    sma_value: float
    distance_pct: float
    prev_open: float
    prev_close: float
    prev_high: float
    prev_low: float
    green_days: int
    red_days: int
    rsi: float | None

    @property
    def strength(self) -> str:
        abs_dist = abs(self.distance_pct)
        if abs_dist < 0.5:
            return "Weak"
        elif abs_dist < 1.5:
            return "Moderate"
        else:
            return "Strong"

    @property
    def rsi_signal(self) -> str:
        if self.rsi is None:
            return "N/A"
        if self.rsi >= 70:
            return "Overbought"
        if self.rsi <= 30:
            return "Oversold"
        return "Neutral"


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
    timestamp: str  # ET display string
    created_at: str = ""  # UTC ISO — machine-comparable entry time
    filters: list[FilterStatus] = field(default_factory=list)
    # Entry-timing guidance: the broken box edge. Pullbacks from the signal
    # close back toward this level are routine after a break-and-retest and
    # don't touch the thesis (only the stop does) — often the better fill.
    retest_level: float = 0.0


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


@dataclass
class DirectionForecast:
    ticker: str
    direction: str  # "BULLISH", "BEARISH", "NEUTRAL", or "VOLATILE" (mixed signals — caution)
    confidence: float  # 0-100
    timestamp: str
    current_price: float
    vwap: float | None
    rsi_30m: float | None
    ema_direction: str | None
    macd_direction: str | None
    divergence: str | None  # "BULLISH_DIV", "BEARISH_DIV", or None
    signals: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    divergence_tf: str | None = None  # timeframe that showed it, e.g. "15m"/"5m"
    rsi_caution: str | None = None    # RSI reversal watch/trigger caution line
    lean: str | None = None           # winning side when direction == VOLATILE
    interval: int = FORECAST_INTERVAL_MINUTES  # candle interval/window (minutes)


def _rsi_caution(prev: float | None, cur: float | None) -> str | None:
    """Caution line when RSI is at an extreme or just left one.

    Replaces the standalone RSI watcher pings (threshold watch/trigger):
    the same information now rides on the forecast so one message tells a
    coherent story instead of two contradicting each other.
    """
    if cur is None:
        return None
    if prev is not None and prev >= RSI_OB_WARN > cur:
        return f"RSI {cur:.0f} fell back out of overbought — bearish reversal trigger"
    if prev is not None and prev <= RSI_OS_WARN < cur:
        return f"RSI {cur:.0f} rose back out of oversold — bullish reversal trigger"
    if cur >= RSI_OB_WARN:
        return f"RSI {cur:.0f} overbought — late to chase calls; pullback risk"
    if cur <= RSI_OS_WARN:
        return f"RSI {cur:.0f} oversold — late to chase puts; bounce risk"
    return None


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
        self.monitoring_started_posted: bool = False
        self.monitoring_ended: bool = False
        self.atr_value: float | None = None
        self.first_candle_overextended: bool = False
        self.active_trade: TradeSignal | None = None
        self.trade_outcome: TradeOutcome | None = None
        self.filter_cooldown_until: datetime | None = None
        self.filtered_signals: list[FilteredSignal] = []
        self.last_forecast_time: datetime | None = None
        self.last_forecast: DirectionForecast | None = None
        self.last_forecast_5m_time: datetime | None = None
        self.day_forecast: DayForecast | None = None
        self.day_forecast_attempts: int = 0  # mid-day recovery tries (capped)
        self.forecast_eval_done: bool = False
        self.bias_logged: bool = False  # bias persisted/posted (survives via rehydration)

    def reset(self, today: str | None = None):
        self.__init__()
        self.trading_day = today


# ── Core strategy functions ─────────────────────────────────

async def get_daily_bias(client: MarketDataClient, ticker: str, today_open: float | None = None,
                         daily: pd.DataFrame | None = None) -> DailyBias | None:
    # `daily` may be supplied pre-fetched (e.g. by the backfill replay) so the
    # same logic runs as-of a historical day; otherwise fetch the latest.
    if daily is None:
        daily = await client.fetch_candles(ticker, interval="D", count=SMA_PERIOD + 10)
    if daily.empty or len(daily) < SMA_PERIOD:
        return None

    daily["SMA"] = daily["close"].rolling(window=SMA_PERIOD).mean()
    latest = daily.iloc[-1]
    sma_val = float(latest["SMA"])
    close_val = float(latest["close"])
    distance_pct = ((close_val - sma_val) / sma_val) * 100

    trend = "BULLISH" if close_val > sma_val else "BEARISH"

    prev = daily.iloc[-2] if len(daily) >= 2 else latest
    prev_open = float(prev["open"])
    prev_close = float(prev["close"])
    prev_high = float(prev["high"])
    prev_low = float(prev["low"])

    # Gap analysis: today's open vs yesterday's close
    if today_open is not None and prev_close > 0:
        gap_pct = round(((today_open - prev_close) / prev_close) * 100, 2)
        if gap_pct > 0.1:
            gap_direction = "GAP_UP"
        elif gap_pct < -0.1:
            gap_direction = "GAP_DOWN"
        else:
            gap_direction = "FLAT"
    else:
        gap_pct = 0.0
        gap_direction = "FLAT"

    # Multi-day trend
    last_5 = daily.tail(5)
    green_days = int((last_5["close"].astype(float) > last_5["open"].astype(float)).sum())
    red_days = 5 - green_days
    if green_days >= 4:
        multi_day = "BULLISH"
    elif red_days >= 4:
        multi_day = "BEARISH"
    else:
        multi_day = "NEUTRAL"

    rsi = _compute_rsi(daily["close"].astype(float))

    # Direction is initially set from gap; gets overridden by first candle later
    if gap_direction == "GAP_UP":
        direction = "BULLISH"
    elif gap_direction == "GAP_DOWN":
        direction = "BEARISH"
    else:
        direction = trend

    return DailyBias(
        ticker=ticker,
        direction=direction,
        trend=trend,
        gap_direction=gap_direction,
        gap_pct=gap_pct,
        multi_day=multi_day,
        current_price=round(close_val, 2),
        sma_value=round(sma_val, 2),
        distance_pct=round(distance_pct, 2),
        prev_open=round(prev_open, 2),
        prev_close=round(prev_close, 2),
        prev_high=round(prev_high, 2),
        prev_low=round(prev_low, 2),
        green_days=green_days,
        red_days=red_days,
        rsi=round(rsi, 1) if rsi is not None else None,
    )


def set_bias_from_first_candle(bias: DailyBias, first_candle: FirstCandleRange) -> None:
    """Override bias direction using first candle + gap + RSI."""
    fc_bullish = first_candle.close_price > first_candle.open_price

    # RSI contrarian: overbought flips bias bearish, oversold flips bullish
    if bias.rsi is not None and bias.rsi >= 70:
        bias.direction = "BEARISH"
        return
    if bias.rsi is not None and bias.rsi <= 30:
        bias.direction = "BULLISH"
        return

    # First candle direction is primary, gap confirms
    if fc_bullish and bias.gap_direction != "GAP_DOWN":
        bias.direction = "BULLISH"
    elif not fc_bullish and bias.gap_direction != "GAP_UP":
        bias.direction = "BEARISH"
    elif fc_bullish:
        bias.direction = "BULLISH"
    else:
        bias.direction = "BEARISH"


@dataclass
class DayForecast:
    """Morning outlook: bias direction plus an ATR-projected high/low band."""
    ticker: str
    direction: str
    strength: str            # from DailyBias (SMA distance)
    open_price: float
    exp_high: float          # projected session high (estimate)
    exp_low: float           # projected session low (estimate)
    day_atr: float           # daily ATR(14)
    pivot: float             # classic floor-trader pivots from prev day
    r1: float
    s1: float
    prev_high: float
    prev_low: float
    timestamp: str           # ET display string


async def compute_day_forecast(client: MarketDataClient, ticker: str,
                               bias: DailyBias, first_candle: FirstCandleRange) -> DayForecast | None:
    """Project today's likely high/low from the daily ATR, skewed by the bias.

    The daily range tends toward ~1 ATR; with a directional bias more of it
    lands on the bias side of the open. Pivots come from yesterday's OHLC.
    """
    daily = await client.fetch_candles(ticker, interval="D", count=ATR_PERIOD * 3)
    if daily.empty or len(daily) < ATR_PERIOD + 2:
        return None

    # Completed days only — during the session providers include today's
    # in-progress daily bar.
    timestamps = pd.to_datetime(daily["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    today = datetime.now(ET).date()
    daily = daily[(timestamps.dt.tz_convert(ET).dt.date < today).values]
    if len(daily) < ATR_PERIOD + 1:
        return None

    day_atr = float(_atr_series(daily, ATR_PERIOD).iloc[-1])
    if np.isnan(day_atr) or day_atr <= 0:
        return None

    open_price = first_candle.open_price
    if bias.direction == "BULLISH":
        up_share, down_share = 0.70, 0.30
    elif bias.direction == "BEARISH":
        up_share, down_share = 0.30, 0.70
    else:
        up_share = down_share = 0.50

    pivot = (bias.prev_high + bias.prev_low + bias.prev_close) / 3
    return DayForecast(
        ticker=ticker,
        direction=bias.direction,
        strength=bias.strength,
        open_price=round(open_price, 2),
        exp_high=round(open_price + day_atr * up_share, 2),
        exp_low=round(open_price - day_atr * down_share, 2),
        day_atr=round(day_atr, 2),
        pivot=round(pivot, 2),
        r1=round(2 * pivot - bias.prev_low, 2),
        s1=round(2 * pivot - bias.prev_high, 2),
        prev_high=bias.prev_high,
        prev_low=bias.prev_low,
        timestamp=datetime.now(ET).strftime("%I:%M %p ET"),
    )


def _compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    val = float(rsi_series(closes, period).iloc[-1])
    return val if not np.isnan(val) else None


async def get_first_candle_range(client: MarketDataClient, ticker: str) -> FirstCandleRange | None:
    data = await client.fetch_candles(ticker, interval="5", count=100)
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
    """Compute ATR from regular-session 5-min candles only."""
    data = await client.fetch_candles(ticker, interval="5", count=200)
    if data.empty:
        return None

    # Filter to regular trading hours (9:30-16:00 ET) to avoid after-hours/pre-market noise
    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC").dt.tz_convert(ET)
    else:
        timestamps = timestamps.dt.tz_convert(ET)
    mask = (timestamps.dt.hour >= 9) & (timestamps.dt.hour < 16)
    mask = mask | ((timestamps.dt.hour == 9) & (timestamps.dt.minute >= 30))
    mask = mask & ~((timestamps.dt.hour == 9) & (timestamps.dt.minute < 30))
    data = data[mask.values].reset_index(drop=True)

    if len(data) < period + 1:
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

    # Previous day high/low as S/R
    if state.daily_bias is not None:
        bias = state.daily_bias
        if direction == "LONG" and entry_price > bias.prev_high:
            filters.append(FilterStatus(name="Prev Day", passed=True,
                reason=f"Entry ${entry_price} > Prev High ${bias.prev_high} (breakout)"))
        elif direction == "SHORT" and entry_price < bias.prev_low:
            filters.append(FilterStatus(name="Prev Day", passed=True,
                reason=f"Entry ${entry_price} < Prev Low ${bias.prev_low} (breakdown)"))
        else:
            filters.append(FilterStatus(name="Prev Day", passed=True,
                reason=f"Inside prev range ${bias.prev_low}-${bias.prev_high} (neutral)"))

    # Daily RSI — warn but don't block: passed stays True so an extreme daily
    # RSI can't veto the signal; the reason text carries the caution.
    if state.daily_bias is not None and state.daily_bias.rsi is not None:
        rsi = state.daily_bias.rsi
        if direction == "LONG" and rsi >= 70:
            filters.append(FilterStatus(name="RSI", passed=True,
                reason=f"RSI {rsi} >= 70 (overbought — caution on longs)"))
        elif direction == "SHORT" and rsi <= 30:
            filters.append(FilterStatus(name="RSI", passed=True,
                reason=f"RSI {rsi} <= 30 (oversold — caution on shorts)"))
        else:
            filters.append(FilterStatus(name="RSI", passed=True,
                reason=f"RSI {rsi} ({state.daily_bias.rsi_signal})"))

    # Multi-day trend confluence
    if state.daily_bias is not None:
        md = state.daily_bias
        if direction == "LONG":
            passed = md.multi_day != "BEARISH"
        else:
            passed = md.multi_day != "BULLISH"
        filters.append(FilterStatus(name="5-Day", passed=passed,
            reason=f"{md.green_days} green / {md.red_days} red ({md.multi_day})"))

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
    data = await client.fetch_candles(ticker, interval="1", count=10)
    if data.empty:
        return None

    # Only bars that started at/after entry count: on the first check most of
    # the window predates the trade, and a pre-entry wick through the stop
    # must not close it.
    if trade.created_at:
        entry_ts = pd.Timestamp(trade.created_at)
        timestamps = pd.to_datetime(data["timestamp"])
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize("UTC")
        data = data[(timestamps >= entry_ts).values]
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
    if data.empty:
        return None

    # Only today's regular session counts: a 200-bar window reaches into the
    # prior session, and after a gap yesterday's closes sit outside today's
    # box — scanning them would spuriously arm broke_above/broke_below.
    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC").dt.tz_convert(ET)
    else:
        timestamps = timestamps.dt.tz_convert(ET)
    session_open = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    candles = data[(timestamps >= session_open).values]
    if len(candles) < 3:
        return None

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
    # If a timeframe fetch fails its filter is skipped, not failed — but record
    # the skip so the filter log shows what was actually checked.
    candles_5m = None
    candles_15m = None
    skipped: list[FilterStatus] = []
    try:
        candles_5m = await client.fetch_candles(ticker, interval="5", count=100)
    except Exception as exc:
        logger.warning("[%s] 5m candle fetch failed — EMA 5m filter skipped: %s", ticker, exc)
        skipped.append(FilterStatus(name="EMA 5m", passed=True, reason="data unavailable — filter skipped"))
    try:
        candles_15m = await client.fetch_candles(ticker, interval="15", count=100)
    except Exception as exc:
        logger.warning("[%s] 15m candle fetch failed — EMA 15m filter skipped: %s", ticker, exc)
        skipped.append(FilterStatus(name="EMA 15m", passed=True, reason="data unavailable — filter skipped"))

    filters = _apply_filters(state, direction, entry_price, candles_1m, candles_5m, candles_15m)
    filters.extend(skipped)
    all_passed = all(f.passed for f in filters)

    if all_passed:
        target = entry_price - (risk * RISK_REWARD_RATIO) if direction == "SHORT" else entry_price + (risk * RISK_REWARD_RATIO)
        state.signal_fired = True
        retest_level = 0.0
        if state.first_candle is not None:
            retest_level = state.first_candle.high if direction == "LONG" else state.first_candle.low
        signal = TradeSignal(
            ticker=ticker, direction=direction,
            entry_price=round(entry_price, 2), stop_loss=round(stop_loss, 2),
            target=round(target, 2), risk=round(risk, 2),
            reward=round(risk * RISK_REWARD_RATIO, 2),
            timestamp=timestamp,
            created_at=datetime.now(pytz.UTC).isoformat(),
            filters=filters,
            retest_level=round(retest_level, 2),
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


# ── 30-min forecast ─────────────────────────────────────────

def _detect_divergence(closes: pd.Series, rsi_values: pd.Series,
                       lookback: int = RSI_DIV_LOOKBACK,
                       order: int = RSI_PIVOT_ORDER) -> str | None:
    """Pivot-based RSI divergence (same detection as rsi_watcher).

    Bearish: the last two price swing highs rise while RSI at those highs falls.
    Bullish: the last two price swing lows fall while RSI at those lows rises.
    """
    window = min(lookback, len(closes))
    if window < 2 * order + 3:
        return None
    c = closes.iloc[-window:].to_numpy()
    r = rsi_values.iloc[-window:].to_numpy()
    if np.isnan(r).all():
        return None
    p_highs, p_lows = _pivots(c, order)

    if len(p_highs) >= 2:
        a, b = p_highs[-2], p_highs[-1]
        if c[b] > c[a] and r[b] < r[a] and not np.isnan(r[b]):
            return "BEARISH_DIV"
    if len(p_lows) >= 2:
        a, b = p_lows[-2], p_lows[-1]
        if c[b] < c[a] and r[b] > r[a] and not np.isnan(r[b]):
            return "BULLISH_DIV"
    return None


async def compute_30m_forecast(client: MarketDataClient, ticker: str,
                               data: pd.DataFrame | None = None,
                               asof: datetime | None = None,
                               interval: int | None = None) -> DirectionForecast | None:
    """Compute a weighted directional forecast from intraday candle data.

    `interval` is the candle interval and forecast window in minutes,
    defaulting to FORECAST_INTERVAL_MINUTES (default 15m; the function keeps
    its historical "30m" name). Pass interval=5 for the fast companion
    forecast. `data`/`asof` may be supplied by the backfill replay so the
    identical computation runs on a historical window as-of a past timestamp.
    """
    if interval is None:
        interval = FORECAST_INTERVAL_MINUTES
    if data is None:
        data = await client.fetch_candles(ticker, interval=str(interval), count=50)
    if data.empty or len(data) < 15:
        return None

    ref_now = asof or datetime.now(ET)

    closes = data["close"].astype(float)
    highs = data["high"].astype(float)
    lows = data["low"].astype(float)
    opens = data["open"].astype(float)
    volumes = data["volume"].astype(float)

    current_price = round(float(closes.iloc[-1]), 2)
    now_str = ref_now.strftime("%I:%M %p ET")

    # Defaults + live-tuned overrides (data/indicator_weights.json)
    indicator_weights = {**INDICATOR_WEIGHTS, **_weight_overrides()}

    signals = {}
    weights = {}

    # RSI
    rsi_vals = rsi_series(closes, 14)
    rsi_val = float(rsi_vals.iloc[-1]) if not np.isnan(rsi_vals.iloc[-1]) else None
    rsi_prev = (float(rsi_vals.iloc[-2])
                if len(rsi_vals) >= 2 and not np.isnan(rsi_vals.iloc[-2]) else None)

    if rsi_val is not None:
        if rsi_val > 60:
            signals["RSI"] = "BULLISH"
        elif rsi_val < 40:
            signals["RSI"] = "BEARISH"
        else:
            signals["RSI"] = "NEUTRAL"
        weights["RSI"] = indicator_weights["RSI"]

    # EMA 9/21
    ema9 = closes.ewm(span=9, adjust=False).mean()
    ema21 = closes.ewm(span=21, adjust=False).mean()
    ema9_val = float(ema9.iloc[-1])
    ema21_val = float(ema21.iloc[-1])
    ema_dir = "BULLISH" if ema9_val > ema21_val else "BEARISH"
    signals["EMA"] = ema_dir
    weights["EMA"] = indicator_weights["EMA"]

    # VWAP
    today = ref_now.date()
    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC").dt.tz_convert(ET)
    else:
        timestamps = timestamps.dt.tz_convert(ET)
    today_mask = timestamps.dt.date == today
    today_data = data[today_mask.values]
    vwap_val = compute_vwap(today_data)
    if vwap_val is not None:
        signals["VWAP"] = "BULLISH" if current_price > vwap_val else "BEARISH"
        weights["VWAP"] = indicator_weights["VWAP"]

    # MACD
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal
    macd_val = float(macd_hist.iloc[-1])
    macd_dir = "BULLISH" if macd_val > 0 else "BEARISH"
    signals["MACD"] = macd_dir
    weights["MACD"] = indicator_weights["MACD"]

    # MACD momentum
    if len(macd_hist) >= 2:
        macd_prev = float(macd_hist.iloc[-2])
        signals["MACD_Mom"] = "BULLISH" if macd_val > macd_prev else "BEARISH"
        weights["MACD_Mom"] = indicator_weights["MACD_Mom"]

    # Divergence — pivot-based reversal signal, voted at face value.
    # (The old endpoint-compare detector was inverted per its backtest; that
    # inversion doesn't carry over to the pivot detector.)
    # The RSI watcher no longer pings divergence standalone (it contradicted
    # forecasts posted moments earlier) — instead the forecast checks its own
    # frame first, then falls back to 5m so the faster frame's divergence
    # still enters the weighted vote. Live only: the backfill replay can't
    # align a second timeframe.
    divergence = _detect_divergence(closes, rsi_vals) if rsi_val is not None else None
    divergence_tf = f"{interval}m" if divergence else None
    if divergence is None and asof is None and client is not None and interval != 5:
        try:
            data_5m = await client.fetch_candles(ticker, interval="5", count=60)
            if data_5m is not None and len(data_5m) >= 15:
                closes_5m = data_5m["close"].astype(float)
                rsi_5m = rsi_series(closes_5m, 14)
                cur_5m = float(rsi_5m.iloc[-1])
                div_5m = None if np.isnan(cur_5m) else _detect_divergence(closes_5m, rsi_5m)
                # Watcher-style gate: only while RSI is still on the reversal
                # side of 50 — past it the move already happened.
                if ((div_5m == "BEARISH_DIV" and cur_5m >= RSI_DIV_RSI_GATE)
                        or (div_5m == "BULLISH_DIV" and cur_5m <= RSI_DIV_RSI_GATE)):
                    divergence, divergence_tf = div_5m, "5m"
        except Exception:
            pass
    if divergence == "BULLISH_DIV":
        signals["Divergence"] = "BULLISH"
        weights["Divergence"] = indicator_weights["Divergence"]
    elif divergence == "BEARISH_DIV":
        signals["Divergence"] = "BEARISH"
        weights["Divergence"] = indicator_weights["Divergence"]

    # Volume trend
    if len(volumes) >= 6:
        recent_avg = volumes.iloc[-3:].mean()
        prior_avg = volumes.iloc[-6:-3].mean()
        if prior_avg > 0:
            vol_ratio = recent_avg / prior_avg
            if vol_ratio > 1.3:
                # High volume — direction follows current candle
                last_dir = "BULLISH" if closes.iloc[-1] > opens.iloc[-1] else "BEARISH"
                signals["Volume"] = last_dir
                weights["Volume"] = indicator_weights["Volume"]

    # Momentum
    if len(closes) >= 3:
        mom = float(closes.iloc[-1] - closes.iloc[-3])
        signals["Momentum"] = "BULLISH" if mom > 0 else "BEARISH"
        weights["Momentum"] = indicator_weights["Momentum"]

    # VIX (inverse): rising VIX -> SPY bearish, falling -> bullish.
    # Live only — skipped when replaying historical data (asof set) since the
    # backfill can't align a historical VIX series.
    if asof is None:
        try:
            vix_sig = await vix.forecast_signal()
            if vix_sig:
                signals["VIX"] = vix_sig
                weights["VIX"] = indicator_weights["VIX"]
        except Exception:
            pass

    # Weighted vote. Confidence is the margin of the winning side over ALL
    # reporting indicators (NEUTRAL votes included in the denominator), scaled
    # to 50-100 — so two lone agreeing signals can't print 100% while the rest
    # of the ensemble abstains.
    bull_score = sum(weights.get(k, 0) for k, v in signals.items() if v == "BULLISH")
    bear_score = sum(weights.get(k, 0) for k, v in signals.items() if v == "BEARISH")
    total_weight = sum(weights.values())

    lean = None
    if total_weight == 0 or bull_score == bear_score:
        direction = "NEUTRAL"
        confidence = 50.0
    else:
        direction = "BULLISH" if bull_score > bear_score else "BEARISH"
        margin = abs(bull_score - bear_score) / total_weight
        confidence = round(50.0 + 50.0 * margin, 1)

        # Contradiction guard: when the losing side carries near the winning
        # side's weight, a directional call is a coin flip (ratio >= 0.75
        # graded 27% vs 61% on rescored live history) — flag VOLATILE but
        # keep the winning side as a lean so the read isn't directionless.
        losing_score = min(bull_score, bear_score)
        winning_score = max(bull_score, bear_score)
        if winning_score > 0 and losing_score / winning_score >= FORECAST_CONFLICT_RATIO:
            lean = direction
            direction = "VOLATILE"
        # Low-confidence gate: below FORECAST_VOLATILE_CONFIDENCE a directional
        # call isn't worth acting on either — same VOLATILE treatment.
        elif confidence < FORECAST_VOLATILE_CONFIDENCE:
            lean = direction
            direction = "VOLATILE"

    return DirectionForecast(
        ticker=ticker,
        direction=direction,
        confidence=confidence,
        timestamp=now_str,
        current_price=current_price,
        vwap=vwap_val,
        rsi_30m=round(rsi_val, 1) if rsi_val is not None else None,
        ema_direction=ema_dir,
        macd_direction=macd_dir,
        divergence=divergence,
        signals=signals,
        weights=weights,
        divergence_tf=divergence_tf,
        rsi_caution=_rsi_caution(rsi_prev, rsi_val),
        lean=lean,
        interval=interval,
    )
