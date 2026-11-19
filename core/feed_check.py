"""One-session Alpaca coverage check without Discord or live candle writes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import sys

from core import market_calendar
from core.alpaca_client import AlpacaClient, MarketDataError
from core.session_bars import session_time


def coverage(client, day, now):
    if not market_calendar.is_trading_day(day):
        raise ValueError("Choose a regular or early-close trading date")
    opening = session_time(day, 9, 30)
    closing = session_time(day, *market_calendar.close_time(day))
    # Avoid counting a minute that closed only seconds ago as missing.
    ready = (now - timedelta(seconds=30)).replace(second=0, microsecond=0)
    cutoff = min(ready, closing)
    if cutoff <= opening:
        raise ValueError("No completed minutes for this date yet")

    bars = client.stock_history_ohlc(
        day, end_time=cutoff.astimezone(market_calendar.ET).strftime("%H:%M:%S"),
    )
    expected = (cutoff - opening) // timedelta(minutes=1)
    actual = {bar.timestamp for bar in bars}
    missing = [
        stamp.astimezone(market_calendar.ET).strftime("%H:%M")
        for index in range(expected)
        if (stamp := opening + timedelta(minutes=index)) not in actual
    ]
    return expected, missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Trading date, YYYY-MM-DD")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = args.date or now.astimezone(market_calendar.ET).date()
    if args.date is None:
        if now < session_time(day, 9, 32):
            day -= timedelta(days=1)
        while not market_calendar.is_trading_day(day):
            day -= timedelta(days=1)
    try:
        expected, missing = coverage(AlpacaClient(), day, now)
    except (ValueError, MarketDataError) as exc:
        print(f"Feed check failed: {exc}", file=sys.stderr)
        return 2
    print(f"SPY Alpaca IEX on {day}: {expected - len(missing)}/{expected} completed minutes")
    if missing:
        print("Missing starts (ET): " + ", ".join(missing[:10]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
