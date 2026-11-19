"""Synchronous SPY minute data from Alpaca's IEX feed."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import config
from core import market_data_usage
from core.market_calendar import ET

_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_PAGES = 3
_MINUTE_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:\.0+)?(?:Z|[+-]\d{2}:\d{2})"
)


class MarketDataError(RuntimeError):
    """Unavailable or invalid SPY market data."""


class MarketDataConfigurationError(MarketDataError):
    """Missing API credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API credentials are intended only for the fixed Alpaca data origin.
        return None


@dataclass(frozen=True)
class StockBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class AlpacaClient:
    def __init__(self, *, purpose="unspecified", opener=None):
        self._key = config.ALPACA_API_KEY_ID
        self._secret = config.ALPACA_API_SECRET_KEY
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open
        self._purpose = purpose
        self._last_request_id = None

    def stock_history_ohlc(
        self, session_date: date, *, symbol="SPY", interval="1m",
        start_time="09:30:00", end_time="16:00:00",
    ) -> list[StockBar]:
        if symbol.upper() != "SPY" or interval != "1m":
            raise ValueError("The setup collector supports SPY 1m bars only")
        if type(session_date) is not date:
            raise ValueError("session_date must be a date")
        start, end = (_boundary(session_date, clock) for clock in (start_time, end_time))
        if start >= end:
            raise ValueError("start_time must precede end_time")

        ticket = market_data_usage.begin_request("stock_history_ohlc", self._purpose)
        self._last_request_id = ticket.request_id if ticket else None
        try:
            bars = self._load_bars(start, end)
        except BaseException as exc:
            market_data_usage.finish_request(ticket, error=exc)
            raise
        market_data_usage.finish_request(ticket, rows=len(bars))
        return bars

    def _load_bars(self, start, end):
        if not self._key or not self._secret:
            raise MarketDataConfigurationError("Set both Alpaca API key fields in the local .env")
        params = {
            "symbols": "SPY", "timeframe": "1Min", "feed": "iex",
            "start": start.isoformat(),
            # Alpaca's end is inclusive. The collector's cutoff is exclusive.
            "end": (end - timedelta(seconds=1)).isoformat(),
            "adjustment": "raw", "sort": "asc", "limit": 1000,
        }
        bars = {}
        seen_tokens = set()
        for _ in range(_MAX_PAGES):
            page = self._get_page(params)
            symbols = page.get("bars")
            if not isinstance(symbols, dict) or symbols.keys() - {"SPY"}:
                raise MarketDataError("Alpaca returned an invalid symbol map")
            rows = symbols.get("SPY", [])
            if not isinstance(rows, list):
                raise MarketDataError("Alpaca returned invalid SPY bars")
            for row in rows:
                bar = _parse_bar(row, start, end)
                if bar.timestamp in bars:
                    raise MarketDataError("Alpaca returned a duplicate minute")
                bars[bar.timestamp] = bar
            token = page.get("next_page_token")
            if token is None:
                return [bars[stamp] for stamp in sorted(bars)]
            if not isinstance(token, str) or not token or len(token) > 2048 or token in seen_tokens:
                raise MarketDataError("Alpaca returned invalid pagination")
            seen_tokens.add(token)
            params["page_token"] = token
        raise MarketDataError("Alpaca bars exceeded the page limit")

    def _get_page(self, params):
        request = Request(f"{_BARS_URL}?{urlencode(params)}", headers={
            "Accept": "application/json",
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
        })
        try:
            with self._open(request, timeout=8) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            raise MarketDataError(f"Alpaca bars returned HTTP {exc.code}") from None
        except (OSError, ValueError):
            raise MarketDataError("Alpaca bars request failed") from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise MarketDataError("Alpaca bars response exceeded the size limit")
        try:
            page = json.loads(body)
        except (ValueError, RecursionError):
            raise MarketDataError("Alpaca returned invalid bars JSON") from None
        if not isinstance(page, dict):
            raise MarketDataError("Alpaca returned an invalid bars response")
        return page

    def close(self):
        """HTTP responses are closed per request; no persistent session to close."""


def _boundary(day, clock):
    try:
        parsed = datetime.strptime(clock, "%H:%M:%S").time()
        if parsed.strftime("%H:%M:%S") != clock:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Request times must use HH:MM:SS") from None
    return ET.localize(datetime.combine(day, parsed)).astimezone(timezone.utc)


def _parse_bar(row, start, end):
    if not isinstance(row, dict):
        raise MarketDataError("Alpaca returned an invalid SPY bar")
    raw_stamp = row.get("t")
    if not isinstance(raw_stamp, str) or not _MINUTE_STAMP.fullmatch(raw_stamp):
        raise MarketDataError("Alpaca returned an invalid bar timestamp")
    try:
        stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise MarketDataError("Alpaca returned an invalid bar timestamp") from None
    if not start <= stamp < end:
        raise MarketDataError("Alpaca returned a bar outside the requested minutes")
    prices = [row.get(key) for key in ("o", "h", "l", "c")]
    volume = row.get("v")
    if any(type(value) not in (int, float) for value in prices) or type(volume) is not int:
        raise MarketDataError("Alpaca returned invalid OHLCV")
    try:
        op, high, low, close = (float(price) for price in prices)
        usable_volume = math.isfinite(volume) and volume >= 0
    except OverflowError:
        raise MarketDataError("Alpaca returned invalid OHLCV") from None
    if not usable_volume or not all(0 < price < math.inf for price in (op, high, low, close)):
        raise MarketDataError("Alpaca returned invalid OHLCV")
    if low > min(op, close) or high < max(op, close):
        raise MarketDataError("Alpaca returned inconsistent OHLC values")
    return StockBar(stamp, op, high, low, close, volume)
