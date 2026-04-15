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

from discord_bot import run_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    print("Starting First Candle Strategy Bot...")
    print("Strategy: 50 SMA Daily Bias + First 5-Min Candle Break & Retest")
    print("Data: Twelve Data → Finnhub → yfinance (fallback)")
    print("-" * 50)
    asyncio.run(run_bot())
