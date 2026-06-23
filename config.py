import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))

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

# 30-min forecast
FORECAST_INTERVAL_MINUTES = 30
FORECAST_START_HOUR = 10
FORECAST_START_MINUTE = 0
FORECAST_END_HOUR = 16
FORECAST_END_MINUTE = 0

# Market hours (Eastern Time)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
FIRST_CANDLE_READY_MINUTE = 35  # 9:35 AM ET — first 5-min candle is complete
MONITORING_END_HOUR = 11        # Stop monitoring at 11:00 AM ET
MONITORING_END_MINUTE = 0

# Economic events (Kalshi public market-data API — no key required)
KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
ECON_EVENTS_ENABLED = os.getenv("ECON_EVENTS_ENABLED", "1") not in ("0", "false", "False", "")
ECON_LOOKAHEAD_DAYS = int(os.getenv("ECON_LOOKAHEAD_DAYS", "14"))  # Briefing look-ahead window

# RSI reversal watcher (real-time alerts on 5m + 15m)
RSI_WATCH_ENABLED = os.getenv("RSI_WATCH_ENABLED", "1") not in ("0", "false", "False", "")
RSI_PERIOD = 14
RSI_OB_WARN = 70.0     # overbought warning
RSI_OB_EXTREME = 80.0  # overbought extreme
RSI_OS_WARN = 30.0     # oversold warning
RSI_OS_EXTREME = 20.0  # oversold extreme
RSI_DIV_LOOKBACK = 40  # bars to scan for divergence / failure-swing pivots
RSI_PIVOT_ORDER = 2    # swing strength (bars on each side of a pivot)
RSI_WATCH_INTERVAL_SECONDS = 60  # min seconds between watcher runs

# Auto-commit captured history (data/) to git after market close
GIT_AUTOCOMMIT_ENABLED = os.getenv("GIT_AUTOCOMMIT_ENABLED", "1") not in ("0", "false", "False", "")
GIT_AUTOCOMMIT_PUSH = os.getenv("GIT_AUTOCOMMIT_PUSH", "1") not in ("0", "false", "False", "")

# Polling
POLL_INTERVAL_SECONDS = 60  # How often to check for setups

# Bot command prefix
COMMAND_PREFIX = "!"
