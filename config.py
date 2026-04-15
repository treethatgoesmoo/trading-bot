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
TICKER = "SPY"
SMA_PERIOD = 50
RISK_REWARD_RATIO = 2.0

# Market hours (Eastern Time)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
FIRST_CANDLE_READY_MINUTE = 35  # 9:35 AM ET — first 5-min candle is complete
MONITORING_END_HOUR = 11        # Stop monitoring at 11:00 AM ET
MONITORING_END_MINUTE = 0

# Polling
POLL_INTERVAL_SECONDS = 60  # How often to check for setups

# Bot command prefix
COMMAND_PREFIX = "!"
