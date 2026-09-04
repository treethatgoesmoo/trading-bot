"""Discord market decision-support bot for manual trading.

Active modules provide a graded SPY intraday forecast, descriptive Bollinger
and volume watchers, daily swing signals, and an optional experimental
paper-only SPXW flow shadow. The bot never executes orders.
"""

import asyncio
import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from bot.core import run_bot
from core.paths import LOGS_DIR as LOG_DIR

os.makedirs(LOG_DIR, exist_ok=True)

log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Console handler
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter(log_format))

# Daily rotating file handler — one file per day (logs/bot-2026-05-06.log)
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, "bot.log"),
    when="midnight",
    interval=1,
    backupCount=90,
    atTime=datetime.min.time(),
    encoding="utf-8",
)
file_handler.suffix = "%Y-%m-%d"
file_handler.namer = lambda name: name.replace("bot.log.", "bot-") + ".log"
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(log_format))

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(console)
root_logger.addHandler(file_handler)

if __name__ == "__main__":
    print("Starting Discord Market Decision-Support Bot...")
    print("Features: SPY intraday forecasts + swing signals + optional SPXW paper shadow")
    print("OHLCV data: Twelve Data → Finnhub → yfinance (fallback)")
    print("-" * 50)
    asyncio.run(run_bot())
