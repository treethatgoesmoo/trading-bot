"""
First Candle Strategy - Discord Trading Bot

Based on the Scarface Trades strategy:
1. Daily 50 SMA determines bullish/bearish bias
2. Box out the first 5-minute candle at market open
3. Wait for break-and-retest in the direction of the daily bias
4. Enter with stop loss and 2R target

Data sources: Twelve Data (primary) → Finnhub → yfinance (fallback)
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
    print("Starting First Candle Strategy Bot...")
    print("Strategy: 50 SMA Daily Bias + First 5-Min Candle Break & Retest")
    print("Data: Twelve Data → Finnhub → yfinance (fallback)")
    print("-" * 50)
    asyncio.run(run_bot())
