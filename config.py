import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str = "1") -> bool:
    """Boolean env var: anything but 0/false/empty is on."""
    return os.getenv(name, default) not in ("0", "false", "False", "")


# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

# Feature toggles
ZERO_DTE_ENABLED = _flag("ZERO_DTE_ENABLED")
SWING_ENABLED = _flag("SWING_ENABLED")

# API keys (Twelve Data and Finnhub are primary; yfinance is the free fallback)
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Rate limits
TWELVEDATA_CALLS_PER_MIN = 7
TWELVEDATA_DAILY_LIMIT = 750
FINNHUB_CALLS_PER_MIN = 55

# Trading
TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "SPY").split(",") if t.strip()]
SMA_PERIOD = 50
RISK_REWARD_RATIO = 2.0

# Indicator filter settings
RVOL_THRESHOLD = 1.2
RVOL_LOOKBACK = 10
ATR_PERIOD = 14
ATR_OVEREXTENDED_MULT = 3.0
EMA_FAST = 9
EMA_SLOW = 21

# Filter cooldown
FILTER_COOLDOWN_MINUTES = 5

# Trade tracking
TRADE_TRACKING_END_HOUR = 16
TRADE_TRACKING_END_MINUTE = 0

# Intraday directional forecast cadence (minutes). Also the candle interval
# the forecast is computed on and graded against.
FORECAST_INTERVAL_MINUTES = int(os.getenv("FORECAST_INTERVAL_MINUTES", "15"))
FORECAST_START_HOUR = 10
FORECAST_START_MINUTE = 0
FORECAST_END_HOUR = 16
FORECAST_END_MINUTE = 0

# Morning day forecast: direction + ATR-projected high/low posted once the
# daily bias is set (~9:35 ET).
DAY_FORECAST_ENABLED = _flag("DAY_FORECAST_ENABLED")

# Post the latest git change to the alert channel when the bot (re)starts.
STARTUP_CHANGELOG_ENABLED = _flag("STARTUP_CHANGELOG_ENABLED")

# Market hours (Eastern Time)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
FIRST_CANDLE_READY_MINUTE = 35  # 9:35 AM ET — first 5-min candle is complete
MONITORING_END_HOUR = 11        # Stop monitoring at 11:00 AM ET
MONITORING_END_MINUTE = 0

# Economic events (Kalshi public market-data API — no key required)
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
ECON_EVENTS_ENABLED = _flag("ECON_EVENTS_ENABLED")
ECON_LOOKAHEAD_DAYS = int(os.getenv("ECON_LOOKAHEAD_DAYS", "14"))  # Briefing look-ahead window

# RSI reversal watcher (real-time alerts on 5m + 15m)
RSI_WATCH_ENABLED = _flag("RSI_WATCH_ENABLED")
RSI_PERIOD = 14
RSI_OB_WARN = 70.0     # overbought warning
RSI_OB_EXTREME = 80.0  # overbought extreme
RSI_OS_WARN = 30.0     # oversold warning
RSI_OS_EXTREME = 20.0  # oversold extreme
RSI_DIV_LOOKBACK = 40  # bars to scan for divergence / failure-swing pivots
RSI_PIVOT_ORDER = 2    # swing strength (bars on each side of a pivot)
# Which divergence directions to alert on: both | bullish | bearish | off
RSI_DIVERGENCE_DIRECTIONS = os.getenv("RSI_DIVERGENCE_DIRECTIONS", "both").lower()
RSI_WATCH_INTERVAL_SECONDS = 60  # min seconds between watcher runs
# Anti-spam: identical (timeframe, kind, direction, stage) alerts are
# suppressed for this long — RSI oscillating around 30/70 otherwise pings on
# every crossing, and sliding-window pivots re-fire the same divergence.
RSI_ALERT_COOLDOWN_MINUTES = int(os.getenv("RSI_ALERT_COOLDOWN_MINUTES", "30"))
RSI_DIV_RSI_GATE = 50.0     # divergence pings only while RSI is still on the reversal side
RSI_FS_STALE_MARGIN = 10.0  # failure-swing break further than this past its trigger is stale

# VIX ("fear gauge", inverse to SPY) — confirming signal + spike alerts
VIX_WATCH_ENABLED = _flag("VIX_WATCH_ENABLED")
VIX_ELEVATED = 20.0   # regime thresholds
VIX_HIGH = 25.0
VIX_FEAR = 30.0
VIX_LEVEL_HYSTERESIS = 0.3    # regime cross-back must clear the level by this (no 19.9↔20.2 ping-pong)
VIX_SPIKE_PCT = 5.0           # intraday % surge that triggers a spike alert
VIX_SPIKE_WINDOW_BARS = 3     # over this many 5-min bars (~15 min)
VIX_SPIKE_COOLDOWN_SEC = 900  # min seconds between spike alerts
VIX_SIGNAL_DEADBAND_PCT = 0.3  # VIX move (last two 30m bars) below this is treated as neutral
VIX_FORECAST_WEIGHT = 2.0     # weight of the VIX vote in the 30-min forecast

# Nightly self-tuning: retune forecast + swing indicator weights from graded
# history after each session close (before the git auto-commit).
AUTO_TUNE_ENABLED = _flag("AUTO_TUNE_ENABLED")

# Auto-commit captured history (data/) to git after market close
GIT_AUTOCOMMIT_ENABLED = _flag("GIT_AUTOCOMMIT_ENABLED")
GIT_AUTOCOMMIT_PUSH = _flag("GIT_AUTOCOMMIT_PUSH")
GIT_AUTOCOMMIT_DELAY_MINUTES = 10  # commit this long after session close (lets EOD eval finish)

# ── Swing options feature ────────────────────────────────────────────────
# Daily-timeframe signals on popular names, suggesting 1-2 week option buys.
SWING_TICKERS = [t.strip().upper() for t in os.getenv("SWING_TICKERS", "TSLA,AMD,GOOG,META").split(",") if t.strip()]
SWING_ALERT_CHANNEL_ID = int(os.getenv("SWING_ALERT_CHANNEL_ID", "0")) or ALERT_CHANNEL_ID

# Scan schedule (ET): pre-open scan + midday confirmation check
SWING_PREOPEN_OFFSET_MINUTES = 30   # scan this long before market open (9:00 ET)
SWING_MIDDAY_HOUR = 12
SWING_MIDDAY_MINUTE = 30

# Signal thresholds
SWING_SCORE_THRESHOLD = 4.0     # min |net weighted score| to fire
SWING_MIN_CONFIDENCE = 65.0     # min confidence (%) to fire
SWING_COOLDOWN_DAYS = 3         # no re-signal per ticker+direction while open/recent

# Contract selection
SWING_DTE_MIN = 7
SWING_DTE_MAX = 14
SWING_DTE_FALLBACK_MAX = 21     # widen to this when no expiry in the primary window
SWING_STRIKE_OTM_MAX_PCT = 3.0  # consider strikes from ATM to this % OTM
SWING_MIN_OPEN_INTEREST = 100
SWING_MAX_SPREAD_PCT = 10.0     # wider bid-ask than this -> illiquid warning

# Grading
SWING_TARGET_MOVE_PCT = 3.0     # underlying move that counts as a win
SWING_HORIZON_CAL_DAYS = 14     # grade at min(expiry, signal + this many days)
SWING_ATR_STOP_MULT = 1.5       # invalidation = close -/+ this x daily ATR(14)

# Polling
POLL_INTERVAL_SECONDS = 60  # How often to check for setups

# Bot command prefix
COMMAND_PREFIX = "!"
