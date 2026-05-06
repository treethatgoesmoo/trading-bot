import pandas as pd
from dataclasses import dataclass
from datetime import datetime
import pytz

from market_data import MarketDataClient
from config import SMA_PERIOD, RISK_REWARD_RATIO

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
class TradeSignal:
    ticker: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    stop_loss: float
    target: float
    risk: float
    reward: float
    timestamp: str


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

    def reset(self, today: str | None = None):
        self.__init__()
        self.trading_day = today


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


async def check_for_setup(state: StrategyState, client: MarketDataClient, ticker: str) -> TradeSignal | None:
    """Poll the latest 1-min candles and check for a break-and-retest setup."""
    if state.daily_bias is None or state.first_candle is None:
        return None
    if state.signal_fired:
        return None

    data = await client.fetch_candles(ticker, interval="1", count=10)
    if data.empty or len(data) < 3:
        return None

    candles = data.tail(10)
    box_high = state.first_candle.high
    box_low = state.first_candle.low
    bias = state.daily_bias.direction

    # Check each candle for break detection
    for i in range(len(candles)):
        row = candles.iloc[i]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])

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
                    target = recent_close - (risk * RISK_REWARD_RATIO)
                    state.signal_fired = True
                    return TradeSignal(
                        ticker=ticker,
                        direction="SHORT",
                        entry_price=round(recent_close, 2),
                        stop_loss=round(stop_loss, 2),
                        target=round(target, 2),
                        risk=round(risk, 2),
                        reward=round(risk * RISK_REWARD_RATIO, 2),
                        timestamp=now_str,
                    )

    # LONG setup: broke above + bullish bias + retest near box_high + bullish candle
    if state.broke_above and bias == "BULLISH" and not state.signal_fired:
        prev_low = float(prev["low"])
        if prev_low <= box_high * 1.001:
            if recent_close > recent_open and recent_close > box_high:
                stop_loss = box_low
                risk = recent_close - stop_loss
                if risk > 0:
                    target = recent_close + (risk * RISK_REWARD_RATIO)
                    state.signal_fired = True
                    return TradeSignal(
                        ticker=ticker,
                        direction="LONG",
                        entry_price=round(recent_close, 2),
                        stop_loss=round(stop_loss, 2),
                        target=round(target, 2),
                        risk=round(risk, 2),
                        reward=round(risk * RISK_REWARD_RATIO, 2),
                        timestamp=now_str,
                    )

    return None
