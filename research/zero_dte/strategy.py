import json
import logging
import os
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
import pytz

from research.core.market_data import MarketDataClient
from core.indicators import rsi_series, pivots as _pivots, atr as _atr
from core.paths import INDICATOR_WEIGHTS_FILE as _WEIGHTS_FILE
from research.config import (
    SMA_PERIOD,
    RVOL_THRESHOLD, RVOL_LOOKBACK,
    FORECAST_CONFLICT_RATIO,
    FORECAST_INTERVAL_MINUTES,
    FORECAST_VOLATILE_CONFIDENCE,
    RSI_DIV_LOOKBACK, RSI_PIVOT_ORDER,
    RSI_OB_WARN, RSI_OS_WARN,
    TUNED_WEIGHT_FLOOR,
    VIX_FORECAST_WEIGHT,
)
from research.core import vix

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
    "Volume": 1.0,
    "Momentum": 1.0,
    "VIX": VIX_FORECAST_WEIGHT,
}

_weights_cache: tuple[float, dict] | None = None


def _weight_overrides(interval: int | None = None) -> dict:
    """Tuned per-indicator weights from data/indicator_weights.json, if present.

    Written by tune_weights.py --write; reloaded automatically on file change.
    The tuned file is per-stream ({"15": {...}, "5": {...}} — a historical 5m
    companion stream once graded very differently from the 15m stream, so
    they must not share one weight set); pass the forecast's interval to get
    its stream's weights. A legacy flat file applies to every stream.
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
            _weights_cache = (mtime, data.get("weights", data))
            logger.info("Loaded indicator weight overrides: %s", _weights_cache[1])
        except Exception as exc:
            logger.warning("Failed to load %s: %s", _WEIGHTS_FILE, exc)
            return {}
    weights = _weights_cache[1]
    if weights and all(isinstance(v, dict) for v in weights.values()):
        weights = weights.get(str(interval), {})   # per-interval schema
    return {k: max(float(v), TUNED_WEIGHT_FLOOR) for k, v in weights.items()}


@dataclass
class DailyBias:
    """Morning bias call (50 SMA trend + opening gap + first candle vote).

    No longer computed or posted by the live bot (demoted, then removed
    entirely — see docs/state-of-the-code.md: the 2-of-3 vote graded 50.9%
    directional vs a 52.6% always-BULLISH baseline over 60 days, no
    measurable edge). Kept here only because backfill.py's `--bias-report`
    replay still measures the rule against history on demand; nothing in the
    live bot computes or reads a DailyBias anymore.
    """
    ticker: str
    direction: str  # trading bias: 2-of-3 vote (first candle/gap/trend), or NEUTRAL
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
class FirstCandleRange:
    """The 9:30-9:35 opening 5-min candle box.

    No longer computed by the live bot (it only ever fed the morning bias
    vote and the break-and-retest entry system, both removed). Kept only
    because backfill.py's bias replay still needs it to reconstruct
    set_bias_from_first_candle's 2-of-3 vote historically.
    """
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
    divergence: str | None  # legacy/research field; no longer used by live forecasts
    signals: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    divergence_tf: str | None = None  # timeframe that showed it, e.g. "15m"/"5m"
    rsi_caution: str | None = None    # RSI reversal watch/trigger caution line
    lean: str | None = None           # winning side when direction == VOLATILE
    interval: int = FORECAST_INTERVAL_MINUTES  # candle interval/window (minutes)
    day_high: float | None = None     # actual session high so far (not a projection)
    day_low: float | None = None      # actual session low so far
    atr: float | None = None          # signal-time ATR used by the future grader
    # Descriptive-only state, NOT graded and NOT a claim of predictive edge.
    # backtest_momentum_extension.py tests both as directional/reversal
    # predictors on frozen ATR-normalized intraday history and has not found
    # a robust cross-asset directional edge (see docs/strategy-log.md,
    # 2026-08-15 entry) — they exist here purely as a
    # "what does the current bar look like" readout (velocity + how
    # stretched vs VWAP/RSI), same status as day_high/day_low or the RSI
    # caution line. Never feed these into the weighted vote or grade them as
    # a hit/miss without a fresh backtest clearing the same bar every other
    # stream here had to clear.
    momentum_score: float | None = None    # -100..+100, 3-bar price move in ATR units
    extension_score: float | None = None   # 0..100, RSI/VWAP stretch
    candle_30m_direction: str | None = None  # latest completed 30m candle; context only
    candle_1h_direction: str | None = None   # latest completed 1h candle; context only


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


# ── Daily bias (backfill.py replay only — see DailyBias/FirstCandleRange
# docstrings; nothing in the live bot calls these anymore) ─────────────────

async def get_daily_bias(client: MarketDataClient, ticker: str, today_open: float | None = None,
                         daily: pd.DataFrame | None = None) -> DailyBias | None:
    # `daily` may be supplied pre-fetched (e.g. by the backfill replay) so the
    # same logic runs as-of a historical day; otherwise fetch the latest.
    if daily is None:
        daily = await client.fetch_candles(ticker, interval="D", count=SMA_PERIOD + 10)
    if daily.empty:
        return None

    # Completed days only — during the session providers include today's
    # in-progress daily bar, which would leak today's (partial) close into the
    # trend/RSI/multi-day components; a provider that omits it would instead
    # shift prev_* (gap base, pivots) onto the wrong day. The backfill replay
    # must truncate its frame the same way (dates strictly before the replay
    # day) so live and replayed bias see identical information.
    timestamps = pd.to_datetime(daily["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    today = datetime.now(ET).date()
    daily = daily[(timestamps.dt.tz_convert(ET).dt.date < today).values].copy()
    if len(daily) < SMA_PERIOD:
        return None

    daily["SMA"] = daily["close"].rolling(window=SMA_PERIOD).mean()
    latest = daily.iloc[-1]          # last completed session (yesterday)
    sma_val = float(latest["SMA"])
    close_val = float(latest["close"])
    distance_pct = ((close_val - sma_val) / sma_val) * 100

    trend = "BULLISH" if close_val > sma_val else "BEARISH"

    prev_open = float(latest["open"])
    prev_close = float(latest["close"])
    prev_high = float(latest["high"])
    prev_low = float(latest["low"])

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
    """Set bias direction by 2-of-3 vote: first candle, gap, 50 SMA trend.

    The old rule let the first candle win unconditionally, which graded a coin
    flip (50% over 60 days; bearish calls 43%). Requiring agreement from at
    least one of gap/trend fixed bearish precision; when all three conflict the
    day is NEUTRAL.
    """
    # RSI contrarian: overbought flips bias bearish, oversold flips bullish
    if bias.rsi is not None and bias.rsi >= 70:
        bias.direction = "BEARISH"
        return
    if bias.rsi is not None and bias.rsi <= 30:
        bias.direction = "BULLISH"
        return

    fc = "BULLISH" if first_candle.close_price > first_candle.open_price else "BEARISH"
    gap = {"GAP_UP": "BULLISH", "GAP_DOWN": "BEARISH"}.get(bias.gap_direction)
    trend = bias.trend

    if fc == gap or fc == trend:
        bias.direction = fc
    elif gap is not None and gap == trend:
        bias.direction = gap
    else:
        bias.direction = "NEUTRAL"


def _compute_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    val = float(rsi_series(closes, period).iloc[-1])
    return val if not np.isnan(val) else None


# ── Indicator functions ─────────────────────────────────────

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
    """(above_threshold, ratio) of the latest bar's volume vs its trailing
    average. Used by the volume-spike watcher (zero_dte/volume_watcher.py);
    previously also fed the break-and-retest RVOL entry filter (removed)."""
    if len(candles) < lookback + 1:
        return True, 0.0

    volumes = candles["volume"].astype(float)
    avg_vol = volumes.iloc[-(lookback + 1):-1].mean()
    if avg_vol == 0:
        return True, 0.0

    latest_vol = volumes.iloc[-1]
    ratio = round(float(latest_vol / avg_vol), 2)
    return ratio >= RVOL_THRESHOLD, ratio


# ── 30-min forecast ─────────────────────────────────────────

# Descriptive-only momentum/extension scoring (NOT graded, NOT a voter in the
# weighted vote below). backtest_momentum_extension.py tested both as
# directional/reversal predictors on real ATR-normalized SPY history and
# found no robust cross-asset directional edge (docs/strategy-log.md,
# 2026-08-15) — they are kept only as a "what does the current bar look like"
# readout, same status as day_high/day_low. Live extension now intentionally
# excludes the rejected divergence component; research must treat logged live
# scores, rather than reconstructed historical scores, as the source of truth.
EXTENSION_ATR_CAP = 3.0    # ATR multiples of price-vs-VWAP distance treated as "fully extended" (score 100)
MOMENTUM_ATR_CAP = 1.0     # ATR multiples of the 3-bar price move treated as "max velocity" (score 100)


def _momentum_extension(closes: pd.Series, rsi_val: float | None, vwap_val: float | None,
                        current_price: float,
                        atr_val: float | None) -> tuple[float | None, float | None]:
    """(momentum_score, extension_score) — see the module-level note above.
    Returns (None, None) when there isn't enough data (short session, no ATR
    yet) rather than a fabricated 0, so callers/embeds can tell "unknown"
    from "flat"."""
    if atr_val is None or atr_val <= 0 or len(closes) < 4:
        return None, None
    mom_raw = float(closes.iloc[-1] - closes.iloc[-4])
    momentum_score = float(np.clip(mom_raw / atr_val / MOMENTUM_ATR_CAP * 100, -100, 100))

    extension_score = None
    if rsi_val is not None:
        rsi_component = abs(rsi_val - 50) / 50 * 100
        vwap_component = 0.0
        if vwap_val:
            vwap_dist_atr = abs(current_price - vwap_val) / atr_val
            vwap_component = float(np.clip(vwap_dist_atr / EXTENSION_ATR_CAP, 0, 1) * 100)
        extension_score = min(100.0, 0.5 * rsi_component + 0.5 * vwap_component)

    return round(momentum_score, 1), (round(extension_score, 1) if extension_score is not None else None)


def _completed_timeframe_direction(candles: pd.DataFrame, base_interval: int,
                                   target_interval: int) -> str | None:
    """Direction of the latest fully assembled regular-session candle.

    Higher-timeframe context is derived from already-fetched completed bars,
    avoiding extra provider calls. It is display-only and never enters the
    weighted forecast vote.
    """
    if candles.empty or target_interval % base_interval or target_interval <= base_interval:
        return None
    timestamps = pd.to_datetime(candles["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    timestamps = timestamps.dt.tz_convert(ET)
    offsets = timestamps.dt.hour * 60 + timestamps.dt.minute - (9 * 60 + 30)
    regular = (offsets >= 0) & (offsets < 390)
    frame = candles.loc[regular.values, ["open", "close"]].copy()
    if frame.empty:
        return None
    frame["session"] = timestamps[regular].dt.date.to_numpy()
    frame["offset"] = offsets[regular].to_numpy()
    frame["bucket"] = (frame["offset"] // target_interval).astype(int)
    needed = target_interval // base_interval
    for _, group in reversed(list(frame.groupby(["session", "bucket"], sort=True))):
        group = group.sort_values("offset")
        expected_start = int(group["bucket"].iat[0]) * target_interval
        expected_offsets = list(range(expected_start, expected_start + target_interval,
                                      base_interval))
        if len(group) != needed or group["offset"].astype(int).tolist() != expected_offsets:
            continue
        first_open = float(group["open"].iat[0])
        last_close = float(group["close"].iat[-1])
        if last_close > first_open:
            return "BULLISH"
        if last_close < first_open:
            return "BEARISH"
        return "NEUTRAL"
    return None


def _detect_divergence(closes: pd.Series, rsi_values: pd.Series,
                       lookback: int = RSI_DIV_LOOKBACK,
                       order: int = RSI_PIVOT_ORDER) -> str | None:
    """Pivot-based RSI divergence.

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
    its historical "30m" name). `data`/`asof` may be supplied by the backfill
    replay so the identical computation runs on a historical window as-of a
    past timestamp.
    """
    if interval is None:
        interval = FORECAST_INTERVAL_MINUTES
    if data is None:
        data = await client.fetch_candles(ticker, interval=str(interval), count=50)
    if data is None or data.empty:
        return None

    ref_now = asof or datetime.now(ET)
    # Providers include the bar currently being formed. A forecast at 10:01
    # must not use the eventual 10:00-10:15 close as an input and then be
    # graded on that same close. Retain only bars whose full interval ended
    # by the forecast time. This also makes historical replay obey the exact
    # same information boundary as the live bot.
    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    ref_ts = pd.Timestamp(ref_now)
    if ref_ts.tzinfo is None:
        ref_ts = ref_ts.tz_localize(ET)
    ref_ts = ref_ts.tz_convert("UTC")
    data = data[(timestamps + pd.Timedelta(minutes=interval) <= ref_ts).values].copy()
    if len(data) < 15:
        return None

    closes = data["close"].astype(float)
    highs = data["high"].astype(float)
    lows = data["low"].astype(float)
    opens = data["open"].astype(float)
    volumes = data["volume"].astype(float)

    current_price = round(float(closes.iloc[-1]), 2)
    now_str = ref_now.strftime("%I:%M %p ET")

    # Actual session high/low so far — the tracked fact that replaced the
    # ATR-projected day forecast (removed 2026-08-12 at the owner's request).
    day_high = day_low = None
    _ts = pd.to_datetime(data["timestamp"])
    if _ts.dt.tz is None:
        _ts = _ts.dt.tz_localize("UTC")
    _ts_et = _ts.dt.tz_convert(ET)
    session_start = ref_now.replace(hour=9, minute=30, second=0, microsecond=0)
    session_mask = ((_ts_et.dt.date == ref_now.date()) & (_ts_et >= session_start)).values
    if session_mask.any():
        day_high = round(float(highs[session_mask].max()), 2)
        day_low = round(float(lows[session_mask].min()), 2)

    # Defaults + live-tuned overrides for THIS stream (data/indicator_weights.json)
    indicator_weights = {**INDICATOR_WEIGHTS, **_weight_overrides(interval)}

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
    today_mask = ((timestamps.dt.date == today) & (timestamps >= session_start))
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

    # RSI divergence was removed from the live read and vote: its historical
    # accuracy did not justify another directional label. The detector remains
    # below only so frozen research scripts can reproduce the rejected test.
    divergence = None
    divergence_tf = None

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

    # Descriptive-only momentum/extension readout (see module-level note near
    # _momentum_extension) — computed here where RSI/VWAP/divergence/ATR are
    # already in scope, never fed into signals/weights above.
    atr_val = float(_atr(data, 14).iloc[-1]) if len(data) >= 14 else None
    if atr_val is not None and np.isnan(atr_val):
        atr_val = None
    # Velocity is explicitly intraday: all four closes must come from today's
    # session. The 10:00/10:15 reads therefore remain unknown rather than
    # blending yesterday's closing bars into an "intraday" score.
    score_closes = today_data["close"].astype(float)
    momentum_score, extension_score = _momentum_extension(
        score_closes, rsi_val, vwap_val, current_price, atr_val)
    candle_30m_direction = _completed_timeframe_direction(data, interval, 30)
    candle_1h_direction = _completed_timeframe_direction(data, interval, 60)

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
        day_high=day_high,
        day_low=day_low,
        atr=(round(atr_val, 4) if atr_val is not None else None),
        momentum_score=momentum_score,
        extension_score=extension_score,
        candle_30m_direction=candle_30m_direction,
        candle_1h_direction=candle_1h_direction,
    )
