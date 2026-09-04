"""Synchronous ThetaData v3 client for SPX index and option snapshots.

The official client connects directly to ThetaData over HTTPS/gRPC; Theta
Terminal and Java are not required.  The package requires Python 3.12+.
Call these methods from async bot code with ``asyncio.to_thread``.

This module deliberately does not turn option activity into a directional
signal.  It provides normalized, executable NBBO data for connectivity checks
and future option-outcome research.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

import pandas as pd

import config

logger = logging.getLogger(__name__)


class ThetaDataError(RuntimeError):
    """Base error for normalized ThetaData failures."""


class ThetaDataConfigurationError(ThetaDataError):
    """ThetaData cannot be initialized in the current environment."""


@dataclass(frozen=True)
class IndexSnapshot:
    symbol: str
    timestamp: datetime | None
    price: float


@dataclass(frozen=True)
class IndexBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiration: date
    strike: float
    right: str
    timestamp: datetime
    bid_size: int
    bid: float
    ask_size: int
    ask: float


@dataclass(frozen=True)
class OptionTradeQuote:
    """An OPRA trade paired by ThetaData with the latest preceding NBBO."""

    symbol: str
    expiration: date
    strike: float
    right: str
    trade_timestamp: datetime
    quote_timestamp: datetime | None
    sequence: int
    condition: int | None
    size: int
    exchange: int | None
    price: float
    bid_size: int
    bid: float
    ask_size: int
    ask: float


@dataclass(frozen=True)
class OptionSnapshot:
    """One SPX/SPXW contract snapshot based on the executable NBBO."""

    symbol: str
    expiration: date
    strike: float
    right: str
    quote_timestamp: datetime | None
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    mid: float
    spread_pct: float | None
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    implied_vol: float | None = None
    greeks_timestamp: datetime | None = None
    underlying_timestamp: datetime | None = None
    underlying_price: float | None = None
    open_interest: int | None = None
    volume: int | None = None

    def quote_age_seconds(self, now: datetime | None = None) -> float | None:
        """Age of the NBBO, or None when ThetaData supplied no timestamp."""
        if self.quote_timestamp is None:
            return None
        now = now or datetime.now(timezone.utc)
        stamp = self.quote_timestamp
        if stamp.tzinfo is None or now.tzinfo is None:
            return None
        age = (now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        return age if age >= 0 else None


class ThetaDataClient:
    """Small adapter around the current ThetaData v3 Python library."""

    def __init__(self, api_key: str | None = None, *, client: Any | None = None) -> None:
        self._api_key = api_key or config.THETADATA_API_KEY
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client is not None or bool(self._api_key)

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
            raise ThetaDataError(f"ThetaData authentication failed: {exc}") from exc
        return self._client

    def subscription_summary(self) -> dict[str, str | None]:
        """Return the entitlements reported by the authenticated client."""
        client = self._get_client()
        return {
            "options": _subscription_name(getattr(client, "options_subscription", None)),
            "index": _subscription_name(getattr(client, "index_subscription", None)),
            "stock": _subscription_name(getattr(client, "stock_subscription", None)),
        }

    def list_expirations(self, symbol: str = "SPXW") -> list[date]:
        """List available option expiration dates for an underlying."""
        try:
            frame = _pandas(self._get_client().option_list_expirations(symbol=symbol.upper()))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData expiration lookup failed for {symbol}: {exc}") from exc
        if frame.empty or "expiration" not in frame:
            return []
        values = pd.to_datetime(frame["expiration"], errors="coerce").dropna()
        return sorted(set(values.dt.date.tolist()))

    def index_snapshots(self, symbols: Iterable[str] = ("SPX", "VIX")) -> list[IndexSnapshot]:
        """Fetch current index prices from the separate Index subscription."""
        requested = [str(symbol).upper() for symbol in symbols]
        try:
            frame = _pandas(self._get_client().index_snapshot_price(symbol=requested))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData index snapshot failed: {exc}") from exc
        snapshots: list[IndexSnapshot] = []
        for _, row in frame.iterrows():
            price = _optional_float(row.get("price"))
            if price is None:
                continue
            snapshots.append(IndexSnapshot(
                symbol=str(row.get("symbol", "")).upper(),
                timestamp=_optional_datetime(row.get("timestamp")),
                price=price,
            ))
        return snapshots

    def index_history_ohlc(
        self,
        session_date: date,
        *,
        symbol: str = "SPX",
        interval: str = "1m",
        start_time: str = "09:30:00",
        end_time: str = "16:00:00",
    ) -> list[IndexBar]:
        """Fetch one regular-session index OHLC series for research."""
        try:
            frame = _pandas(self._get_client().index_history_ohlc(
                symbol=symbol.upper(),
                start_date=session_date,
                end_date=session_date,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
            ))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData index history failed for {symbol}: {exc}") from exc
        bars: list[IndexBar] = []
        for _, row in frame.iterrows():
            stamp = _optional_datetime(row.get("timestamp"))
            values = [_optional_float(row.get(name)) for name in ("open", "high", "low", "close")]
            if stamp is None or any(value is None for value in values):
                continue
            bars.append(IndexBar(stamp, values[0], values[1], values[2], values[3]))
        return sorted(bars, key=lambda bar: bar.timestamp)

    def option_trade_quotes(
        self,
        session_date: date,
        *,
        expiration: date | None = None,
        symbol: str = "SPXW",
        strike_range: int = 20,
        start_time: str = "09:30:00",
        end_time: str = "16:00:00",
    ) -> list[OptionTradeQuote]:
        """Fetch OPRA trades with their preceding NBBO for one session.

        The returned records support aggressor inference, not knowledge of
        opening/closing intent, multi-leg intent, or the full resting book.
        """
        expiration = expiration or session_date
        if strike_range < 0:
            raise ValueError("strike_range must be non-negative")
        try:
            frame = _pandas(self._get_client().option_history_trade_quote(
                symbol=symbol.upper(),
                expiration=expiration,
                date=session_date,
                strike="*",
                right="both",
                strike_range=strike_range,
                start_time=start_time,
                end_time=end_time,
                exclusive=True,
            ))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData option trade/quote history failed: {exc}") from exc

        if frame.empty:
            return []
        # A full OPRA window can contain hundreds of thousands of rows. Parse
        # columns once, then use itertuples; per-row iterrows/pd.to_datetime was
        # prohibitively slow on the deployment Pi.
        required = {
            "symbol", "expiration", "strike", "right", "trade_timestamp",
            "quote_timestamp", "size", "price", "bid", "ask",
        }
        if missing := required.difference(frame.columns):
            raise ThetaDataError(f"ThetaData trade/quote response missing columns: {sorted(missing)}")
        frame["expiration"] = pd.to_datetime(frame["expiration"], errors="coerce").dt.date
        frame["trade_timestamp"] = _utc_timestamp_column(frame["trade_timestamp"])
        frame["quote_timestamp"] = _utc_timestamp_column(frame["quote_timestamp"])
        frame["symbol"] = frame["symbol"].astype(str).str.upper()
        frame["right"] = frame["right"].astype(str).str.lower()
        for column in ("strike", "size", "price", "bid_size", "bid", "ask_size", "ask",
                       "sequence", "condition", "exchange"):
            if column not in frame:
                frame[column] = 0 if column in {"bid_size", "ask_size", "sequence"} else float("nan")
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        valid = (
            frame["expiration"].notna()
            & frame["trade_timestamp"].notna()
            & frame["strike"].notna()
            & frame["price"].notna()
            & frame["size"].gt(0)
            & frame["right"].isin(("call", "put"))
            & frame["symbol"].ne("")
        )
        frame = frame.loc[valid].copy()
        frame["sequence"] = frame["sequence"].fillna(0).astype("int64")
        frame = frame.drop_duplicates(
            subset=["symbol", "expiration", "strike", "right", "trade_timestamp", "sequence", "exchange"]
        ).sort_values(["trade_timestamp", "sequence", "exchange"], na_position="first")

        records: list[OptionTradeQuote] = []
        for row in frame.itertuples(index=False):
            records.append(OptionTradeQuote(
                symbol=row.symbol,
                expiration=row.expiration,
                strike=float(row.strike),
                right=row.right,
                trade_timestamp=row.trade_timestamp.to_pydatetime(),
                quote_timestamp=(row.quote_timestamp.to_pydatetime()
                                 if pd.notna(row.quote_timestamp) else None),
                sequence=int(row.sequence),
                condition=_optional_int(row.condition),
                size=int(row.size),
                exchange=_optional_int(row.exchange),
                price=float(row.price),
                bid_size=_optional_int(row.bid_size) or 0,
                bid=_float_or_zero(row.bid),
                ask_size=_optional_int(row.ask_size) or 0,
                ask=_float_or_zero(row.ask),
            ))
        return records

    def option_quote_history(
        self,
        session_date: date,
        *,
        expiration: date,
        strike: float,
        right: str,
        symbol: str = "SPXW",
        interval: str = "1s",
        start_time: str = "09:30:00",
        end_time: str = "16:00:00",
    ) -> list[OptionQuote]:
        """Fetch quote-time records for exact executable outcome grading."""
        if right not in {"call", "put"}:
            raise ValueError("right must be 'call' or 'put'")
        try:
            frame = _pandas(self._get_client().option_history_quote(
                symbol=symbol.upper(), expiration=expiration, interval=interval,
                date=session_date, strike=str(strike), right=right,
                start_time=start_time, end_time=end_time,
            ))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData option quote history failed: {exc}") from exc
        quotes: list[OptionQuote] = []
        for _, row in frame.iterrows():
            key = _contract_key(row)
            stamp = _optional_datetime(row.get("timestamp"))
            if key is None or stamp is None:
                continue
            contract_symbol, expiry, contract_strike, contract_right = key
            quotes.append(OptionQuote(
                contract_symbol, expiry, contract_strike, contract_right, stamp,
                _optional_int(row.get("bid_size")) or 0, _float_or_zero(row.get("bid")),
                _optional_int(row.get("ask_size")) or 0, _float_or_zero(row.get("ask")),
            ))
        return sorted(quotes, key=lambda item: item.timestamp)

    def option_chain_snapshot(
        self,
        expiration: date,
        *,
        symbol: str = "SPXW",
        right: str = "both",
        strike_range: int = 5,
        include_details: bool = True,
    ) -> list[OptionSnapshot]:
        """Fetch a narrow real-time option chain around ThetaData's ATM strike.

        NBBO quote data is required.  Greeks, open interest, and session volume
        are best-effort additions so a temporary auxiliary endpoint failure does
        not discard otherwise executable quotes.  Standard supplies first-order
        Greeks; gamma is intentionally absent because it is a higher-tier field.
        """
        if right not in {"call", "put", "both"}:
            raise ValueError("right must be 'call', 'put', or 'both'")
        if strike_range < 0:
            raise ValueError("strike_range must be non-negative")

        client = self._get_client()
        params = {
            "symbol": symbol.upper(),
            "expiration": expiration,
            "strike": "*",
            "right": right,
            "strike_range": strike_range,
        }
        try:
            quotes = _pandas(client.option_snapshot_quote(**params))
        except Exception as exc:
            raise ThetaDataError(f"ThetaData option quote snapshot failed: {exc}") from exc
        if quotes.empty:
            return []

        details: dict[str, pd.DataFrame] = {}
        if include_details:
            for name, method_name in (
                ("greeks", "option_snapshot_greeks_first_order"),
                ("open_interest", "option_snapshot_open_interest"),
                ("ohlc", "option_snapshot_ohlc"),
            ):
                try:
                    details[name] = _pandas(getattr(client, method_name)(**params))
                except Exception as exc:
                    logger.warning("ThetaData %s snapshot unavailable: %s", name, exc)
                    details[name] = pd.DataFrame()

        lookup = {name: _row_lookup(frame) for name, frame in details.items()}
        snapshots: list[OptionSnapshot] = []
        for _, quote in quotes.iterrows():
            key = _contract_key(quote)
            if key is None:
                continue
            contract_symbol, expiry, strike, contract_right = key
            bid = _float_or_zero(quote.get("bid"))
            ask = _float_or_zero(quote.get("ask"))
            if ask <= 0 or bid < 0 or ask < bid:
                continue
            mid = (bid + ask) / 2.0
            spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else None
            greek = lookup.get("greeks", {}).get(key, {})
            oi = lookup.get("open_interest", {}).get(key, {})
            ohlc = lookup.get("ohlc", {}).get(key, {})
            snapshots.append(OptionSnapshot(
                symbol=contract_symbol,
                expiration=expiry,
                strike=strike,
                right=contract_right,
                quote_timestamp=_optional_datetime(quote.get("timestamp")),
                bid=bid,
                ask=ask,
                bid_size=int(_float_or_zero(quote.get("bid_size"))),
                ask_size=int(_float_or_zero(quote.get("ask_size"))),
                mid=round(mid, 4),
                spread_pct=round(spread_pct, 2) if spread_pct is not None else None,
                delta=_optional_float(greek.get("delta")),
                theta=_optional_float(greek.get("theta")),
                vega=_optional_float(greek.get("vega")),
                rho=_optional_float(greek.get("rho")),
                implied_vol=_optional_float(greek.get("implied_vol")),
                greeks_timestamp=_optional_datetime(greek.get("timestamp")),
                underlying_timestamp=_optional_datetime(greek.get("underlying_timestamp")),
                underlying_price=_optional_float(greek.get("underlying_price")),
                open_interest=_optional_int(oi.get("open_interest")),
                volume=_optional_int(ohlc.get("volume")),
            ))
        return sorted(snapshots, key=lambda item: (item.strike, item.right))


def _pandas(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        frame = value.copy()
    elif hasattr(value, "to_pandas"):
        frame = value.to_pandas()
    else:
        frame = pd.DataFrame(value)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame


def _contract_key(row: Any) -> tuple[str, date, float, str] | None:
    try:
        expiry = pd.to_datetime(row.get("expiration"), errors="raise").date()
        strike = float(row.get("strike"))
    except (TypeError, ValueError):
        return None
    symbol = str(row.get("symbol", "")).upper()
    right = str(row.get("right", "")).lower()
    if not symbol or not math.isfinite(strike) or right not in {"call", "put"}:
        return None
    return symbol, expiry, strike, right


def _row_lookup(frame: pd.DataFrame) -> dict[tuple[str, date, float, str], dict[str, Any]]:
    result: dict[tuple[str, date, float, str], dict[str, Any]] = {}
    for _, row in frame.iterrows():
        key = _contract_key(row)
        if key is not None:
            result[key] = row.to_dict()
    return result


def _float_or_zero(value: Any) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else 0.0


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None


def _utc_timestamp_column(values: pd.Series) -> pd.Series:
    """Vectorized aware-to-UTC conversion; ambiguous naïve values become NaT."""
    parsed = pd.to_datetime(values, errors="coerce")
    if not isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, UTC]")
    return parsed.dt.tz_convert("UTC")


def _optional_datetime(value: Any) -> datetime | None:
    """Normalize provider timestamps to aware UTC; reject ambiguous naïve time."""
    if value is None or pd.isna(value):
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.tz_convert("UTC").to_pydatetime()


def _subscription_name(value: Any) -> str | None:
    """Translate ThetaData's numeric entitlement codes for diagnostics."""
    text = str(value).strip() if value is not None else ""
    return {"0": "FREE", "1": "VALUE", "2": "STANDARD", "3": "PRO"}.get(text, text or None)


def _diagnose(symbol: str) -> int:
    client = ThetaDataClient()
    try:
        print("Entitlements:", client.subscription_summary())
        prices = client.index_snapshots(("SPX", "VIX"))
        if prices:
            for snapshot in prices:
                print(f"{snapshot.symbol}: {snapshot.price} @ {snapshot.timestamp}")
        else:
            print("Index snapshot: no rows (normal outside the current session cache)")

        expirations = [expiry for expiry in client.list_expirations(symbol) if expiry >= date.today()]
        print(f"{symbol} upcoming expirations:", ", ".join(map(str, expirations[:5])) or "none")
        if date.today() in expirations:
            chain = client.option_chain_snapshot(date.today(), symbol=symbol, strike_range=2)
            print(f"Today's narrow option snapshot: {len(chain)} contracts")
            for item in chain[:6]:
                print(
                    f"  {item.expiration} {item.strike:g} {item.right}: "
                    f"{item.bid:g} x {item.ask:g}, delta={item.delta}, IV={item.implied_vol}"
                )
        else:
            print("No same-day expiration today; live option snapshot skipped")
        return 0
    except ThetaDataError as exc:
        print(f"ThetaData diagnostic failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ThetaData v3 options and index access")
    parser.add_argument("--symbol", default="SPXW", help="option root to inspect (default: SPXW)")
    args = parser.parse_args()
    return _diagnose(args.symbol.upper())


if __name__ == "__main__":
    raise SystemExit(main())
