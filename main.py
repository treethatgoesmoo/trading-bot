"""Private completed-candle SPX bullish/bearish/volatile Discord alerts."""
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from bot.core import run_bot
from core.paths import LOGS_DIR


def configure_logging():
    directory = Path(LOGS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    file_handler = TimedRotatingFileHandler(
        directory / 'bot.log', when='midnight', interval=1, backupCount=30, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler], force=True)


if __name__ == '__main__':
    configure_logging()
    logging.getLogger(__name__).info(
        'Starting private SPX state alerts: completed candles, ThetaData index source, Discord delivery')
    asyncio.run(run_bot())
