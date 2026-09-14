"""Synchronous SPY OHLCV adapter for ThetaData v3 (Python 3.12+).

Only the Nasdaq Basic (nqb) stock feed is requested, consistently for live and
prior sessions. Its volume is feed-specific, not claimed consolidated volume.
Call from the collector's cancellation-safe single-flight worker.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

import config
from core import theta_usage
from core.market_calendar import ET


class ThetaDataError(RuntimeError):
    """Provider failure or unusable provider response."""


class ThetaDataConfigurationError(ThetaDataError):
    """Missing credentials, SDK or supported Python runtime."""


@dataclass(frozen=True)
class StockBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class ThetaDataClient:
    """Small adapter around the current ThetaData v3 Python library."""

    def __init__(
        self, api_key: str | None = None, *, client: Any | None = None,
        purpose: str = "unspecified",
    ) -> None:
        self._api_key = api_key or config.THETADATA_API_KEY
        self._client = client
        self._purpose = purpose if purpose in theta_usage.PURPOSES else "unspecified"
        # Dedicated single-flight collectors copy this immediately after a call.
        # None outside the audit window; never invent persisted request evidence.
        self._last_request_id: str | None = None

    def _request(self, endpoint: str, **kwargs: Any) -> Any:
        """Audit each SDK invocation, before normalization.

        Counts SDK calls, not internal transport retries. Client initialization
        failures are recorded against the endpoint attempted. Telemetry must
        never change the SDK result or exception.
        """
        ticket = theta_usage.begin_request(endpoint, self._purpose)
        self._last_request_id = ticket.request_id if ticket is not None else None
        try:
            result = getattr(self._get_client(), endpoint)(**kwargs)
        except BaseException as exc:
            theta_usage.finish_request(ticket, error=exc)
            raise
        rows = None
        if ticket is not None:
            try:
                # Dataframes/Arrow expose shape or num_rows. Do not consume
                # iterators or copy licensed data merely to count records.
                if hasattr(result, "shape"):
                    rows = int(result.shape[0])
                elif hasattr(result, "num_rows"):
                    rows = int(result.num_rows)
                elif isinstance(result, (list, tuple)):
                    rows = len(result)
                elif result is None:
                    rows = 0
            except Exception:
                pass
        theta_usage.finish_request(ticket, rows=rows)
        return result

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise ThetaDataConfigurationError(
                "THETADATA_API_KEY is not configured; add it to the ignored .env file"
            )
        if sys.version_info < (3, 12):
            raise ThetaDataConfigurationError(
                "ThetaData's v3 Python library requires Python 3.12 or newer"
            )
        try:
            from thetadata import ThetaClient
        except ImportError as exc:
            raise ThetaDataConfigurationError(
                "thetadata is not installed; run python3 -m pip install -r requirements.txt"
            ) from exc
        try:
            self._client = ThetaClient(
                api_key=self._api_key,
                dataframe_type="pandas",
            )
        except Exception as exc:
            raise ThetaDataError("ThetaData authentication failed") from exc
        return self._client

    def stock_history_ohlc(
        self, session_date: date, *, symbol: str = "SPY", interval: str = "1m",
        start_time: str = "09:30:00", end_time: str = "16:00:00",
    ) -> list[StockBar]:
        """Return validated opening-time OHLCV bars, never fabricated volume.

        Malformed responses fail the whole request rather than silently hiding
        a gap. Empty provider responses remain empty. No fallback feed is used.
        """
        if symbol.upper() != "SPY" or interval != "1m":
            raise ValueError("The setup collector supports SPY 1m bars only")
        if type(session_date) is not date:
            raise ValueError("session_date must be a date")
        try:
            frame = _pandas(self._request(
                "stock_history_ohlc", symbol="SPY", interval="1m",
                start_date=session_date, end_date=session_date,
                start_time=start_time, end_time=end_time, venue="nqb",
            ))
        except ThetaDataConfigurationError:
            raise
        except Exception as exc:
            raise ThetaDataError("ThetaData SPY stock history request failed") from exc
        if frame.empty:
            return []
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not frame.columns.is_unique or not required.issubset(frame.columns):
            raise ThetaDataError("ThetaData stock response has missing or duplicate OHLCV columns")
        bars = []
        seen = set()
        for row in frame.to_dict("records"):
            stamp = _timestamp(row["timestamp"])
            values = [_number(row[name]) for name in ("open", "high", "low", "close", "volume")]
            o, h, low, c, volume = values
            if (min(o, h, low, c) <= 0 or volume < 0 or volume != int(volume)
                    or low > min(o, c) or h < max(o, c) or low > h):
                raise ThetaDataError("ThetaData stock response has invalid OHLCV values")
            if (stamp.second or stamp.microsecond or stamp in seen
                    or stamp.astimezone(ET).date() != session_date):
                raise ThetaDataError("ThetaData stock response has invalid or duplicate bar timestamps")
            seen.add(stamp)
            bars.append(StockBar(stamp, o, h, low, c, volume))
        return sorted(bars, key=lambda bar: bar.timestamp)

    def close(self) -> None:
        """Drop the lazy client; invoke public close if a future SDK exposes it.

        SDK 1.0.10 exposes neither close nor its channel. Do not instantiate a
        client just to shut down, or reach into gRPC stub internals. Call only
        after the collector has drained provider work.
        """
        client, self._client = self._client, None
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()


def _pandas(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        frame = value.copy()
    elif hasattr(value, "to_pandas"):
        frame = value.to_pandas().copy()
    else:
        frame = pd.DataFrame(value)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ThetaDataError("Boolean is not an OHLCV number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ThetaDataError("Missing or invalid OHLCV number") from exc
    if not math.isfinite(parsed):
        raise ThetaDataError("Nonfinite OHLCV number")
    return parsed


def _timestamp(value: Any) -> datetime:
    # Numeric epoch values and naive wall times are ambiguous; never guess units
    # or timezone. The SDK normally supplies timezone-aware timestamps.
    if not isinstance(value, (str, datetime, pd.Timestamp)):
        raise ThetaDataError("Invalid stock timestamp")
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed) or parsed.tzinfo is None or parsed.nanosecond:
            raise ValueError("Missing timezone or fractional minute")
        return parsed.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ThetaDataError("Stock timestamp must be aware and valid") from exc
