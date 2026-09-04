"""Collect one frozen SPX/SPY/SPXW session for turning-point research.

Raw OPRA data is written under the ignored ``.thetadata-data`` directory and
must never be committed or redistributed. This command performs no strategy
assessment and sends no Discord message.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from core.market_calendar import ET, close_time, is_trading_day
from core.paths import ROOT, THETADATA_DATA_DIR
from core.thetadata_client import IndexBar, OptionTradeQuote, ThetaDataClient

SCHEMA = 1
OPTION_ROOT = "SPXW"
START_TIME = "09:30:00"
COMPLETION_DELAY_MINUTES = 5


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    materialized = list(rows)
    if not materialized:
        raise RuntimeError(f"provider returned no rows for {path.name}")
    fields = list(materialized[0])
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in materialized:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})
    os.replace(temporary, path)
    return len(materialized)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_data_dir(data_dir: Path) -> Path:
    """Permit only the canonical ignored directory or a path outside the repo."""
    requested = data_dir.expanduser().resolve()
    root = Path(ROOT).resolve()
    canonical = Path(THETADATA_DATA_DIR).resolve()
    inside_root = requested == root or root in requested.parents
    inside_canonical = requested == canonical or canonical in requested.parents
    if inside_root and not inside_canonical:
        raise ValueError(
            f"licensed data path must be under {canonical} or outside the repository; got {requested}"
        )
    return requested


def _verify_existing(directory: Path, expected: dict[str, Any]) -> bool:
    if not directory.exists():
        return False
    metadata_path = directory / "metadata.json"
    if not metadata_path.exists():
        raise RuntimeError(f"incomplete frozen snapshot at {directory}; use --refresh to replace")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("complete") is not True:
        raise RuntimeError(f"frozen snapshot is not marked complete in {metadata_path}")
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise RuntimeError(
                f"frozen snapshot parameter mismatch for {key}: "
                f"stored={metadata.get(key)!r}, requested={value!r}; use --refresh deliberately"
            )
    files = metadata.get("files", {})
    expected_names = {"spx_1m.csv", "spxw_trade_quote.csv"}
    if expected["include_spy"]:
        expected_names.add("spy_1m.csv")
    if set(files) != expected_names:
        raise RuntimeError(f"frozen snapshot file set mismatch in {metadata_path}")
    for filename, details in files.items():
        path = directory / filename
        actual = _sha256(path) if path.exists() else None
        if actual != details.get("sha256"):
            raise RuntimeError(
                f"frozen snapshot checksum mismatch for {path}; use --refresh only to replace deliberately"
            )
    return True


def _spy_rows(session_date: date) -> list[dict[str, Any]]:
    import yfinance as yf

    frame = yf.download(
        "SPY",
        start=session_date.isoformat(),
        end=(session_date + timedelta(days=1)).isoformat(),
        interval="1m",
        auto_adjust=False,
        prepost=False,
        progress=False,
    )
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    time_column = "Datetime" if "Datetime" in frame else "Date"
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        stamp = pd.Timestamp(row[time_column])
        if stamp.tzinfo is None:
            raise RuntimeError("yfinance returned an ambiguous naïve intraday timestamp")
        stamp = stamp.tz_convert("UTC")
        if stamp.tz_convert(ET).date() != session_date:
            continue
        rows.append({
            "timestamp": stamp.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        })
    return rows


def _session_boundary(session_date: date, hour: int, minute: int) -> datetime:
    return ET.localize(datetime(
        session_date.year, session_date.month, session_date.day, hour, minute
    ))


def _validate_collection_time(session_date: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("collection time must be timezone-aware")
    now_et = now.astimezone(ET)
    if session_date > now_et.date():
        raise ValueError(f"cannot collect future session {session_date}")
    close_hour, close_minute = close_time(session_date)
    complete_after = _session_boundary(session_date, close_hour, close_minute) + timedelta(
        minutes=COMPLETION_DELAY_MINUTES
    )
    if session_date == now_et.date() and now_et < complete_after:
        raise RuntimeError(
            f"{session_date} is still in progress; collect after {complete_after.strftime('%H:%M %Z')}"
        )


def _validate_bar_coverage(
    name: str, timestamps: list[datetime], session_date: date, close_hour: int, close_minute: int
) -> None:
    if not timestamps or any(stamp.tzinfo is None for stamp in timestamps):
        raise RuntimeError(f"{name} has no complete timezone-aware coverage")
    local = sorted(stamp.astimezone(ET) for stamp in timestamps)
    if any(stamp.date() != session_date for stamp in local):
        raise RuntimeError(f"{name} contains rows outside {session_date}")
    expected_open = _session_boundary(session_date, 9, 30)
    expected_last = _session_boundary(session_date, close_hour, close_minute) - timedelta(minutes=1)
    tolerance = timedelta(minutes=2)
    if abs(local[0] - expected_open) > tolerance or local[-1] < expected_last - tolerance:
        raise RuntimeError(
            f"{name} is incomplete: observed {local[0].isoformat()} through {local[-1].isoformat()}"
        )


def _validate_session_complete(
    session_date: date,
    index_bars: list[IndexBar],
    option_trades: list[OptionTradeQuote],
    spy_rows: list[dict[str, Any]] | None,
) -> None:
    close_hour, close_minute = close_time(session_date)
    _validate_bar_coverage(
        "SPX", [bar.timestamp for bar in index_bars], session_date, close_hour, close_minute
    )
    if spy_rows is not None:
        stamps = [pd.Timestamp(row["timestamp"]).to_pydatetime() for row in spy_rows]
        _validate_bar_coverage("SPY", stamps, session_date, close_hour, close_minute)
    if not option_trades:
        raise RuntimeError("SPXW trade/quote history returned no rows")
    for trade in option_trades:
        stamp = trade.trade_timestamp
        if stamp.tzinfo is None or stamp.astimezone(ET).date() != session_date:
            raise RuntimeError("SPXW trade/quote history contains an invalid session timestamp")


def _package_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _timestamp_range(rows: list[dict[str, Any]], field: str) -> list[str | None]:
    values = [str(row[field]) for row in rows if row.get(field) is not None]
    return [min(values), max(values)] if values else [None, None]


def _publish_staging(staging: Path, destination: Path) -> None:
    backup: Path | None = None
    if destination.exists():
        backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
        os.replace(destination, backup)
    try:
        os.replace(staging, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def collect(
    session_date: date,
    *,
    data_dir: Path,
    strike_range: int,
    refresh: bool,
    include_spy: bool,
    now: datetime | None = None,
) -> Path:
    if not is_trading_day(session_date):
        raise ValueError(f"{session_date} is not an NYSE trading day")
    if strike_range < 0:
        raise ValueError("strike_range must be non-negative")
    now = now or datetime.now(timezone.utc)
    _validate_collection_time(session_date, now)
    data_dir = _safe_data_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    destination = data_dir / session_date.isoformat()
    close_hour, close_minute = close_time(session_date)
    end_time = f"{close_hour:02d}:{close_minute:02d}:00"
    request = {
        "schema": SCHEMA,
        "session_date": session_date.isoformat(),
        "option_root": OPTION_ROOT,
        "expiration": session_date.isoformat(),
        "strike_range": strike_range,
        "session_window_et": [START_TIME, end_time],
        "include_spy": include_spy,
        "timezone": "UTC",
        "completion_delay_minutes": COMPLETION_DELAY_MINUTES,
    }
    if not refresh and _verify_existing(destination, request):
        print(f"Verified existing frozen snapshot: {destination}")
        return destination

    client = ThetaDataClient()
    expirations = client.list_expirations(OPTION_ROOT)
    if session_date not in expirations:
        raise RuntimeError(f"{OPTION_ROOT} has no expiration on {session_date}")

    index_bars: list[IndexBar] = client.index_history_ohlc(
        session_date, symbol="SPX", interval="1m", end_time=end_time
    )
    option_trades: list[OptionTradeQuote] = client.option_trade_quotes(
        session_date, symbol=OPTION_ROOT, expiration=session_date,
        strike_range=strike_range, end_time=end_time,
    )
    spy_rows = _spy_rows(session_date) if include_spy else None
    _validate_session_complete(session_date, index_bars, option_trades, spy_rows)
    outputs: dict[str, list[dict[str, Any]]] = {
        "spx_1m.csv": [asdict(row) for row in index_bars],
        "spxw_trade_quote.csv": [asdict(row) for row in option_trades],
    }
    if spy_rows is not None:
        outputs["spy_1m.csv"] = spy_rows

    staging = data_dir / f".{session_date.isoformat()}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        files: dict[str, dict[str, Any]] = {}
        for filename, rows in outputs.items():
            path = staging / filename
            count = _write_csv(path, rows)
            time_field = "trade_timestamp" if filename == "spxw_trade_quote.csv" else "timestamp"
            files[filename] = {
                "rows": count,
                "timestamp_range_utc": _timestamp_range(rows, time_field),
                "sha256": _sha256(path),
            }
        entitlements = client.subscription_summary()
        metadata = {
            **request,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "complete": True,
            "thetadata_version": _package_version("thetadata"),
            "yfinance_version": _package_version("yfinance") if include_spy else None,
            "pandas_version": _package_version("pandas"),
            "options_entitlement": entitlements.get("options"),
            "index_entitlement": entitlements.get("index"),
            "spy_provider": "yfinance" if include_spy else None,
            "files": files,
        }
        (staging / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _publish_staging(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(f"Wrote frozen snapshot: {destination}")
    for filename, details in files.items():
        print(f"  {filename}: {details['rows']} rows, sha256={details['sha256']}")
    print("Trade/quote rows support flow inference; exact 15/30m exits require contract quote history.")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze one SPX/SPY/SPXW research session")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="session date YYYY-MM-DD")
    parser.add_argument("--data-dir", type=Path, default=Path(THETADATA_DATA_DIR))
    parser.add_argument("--strike-range", type=int, default=20, help="strikes on each side of ATM")
    parser.add_argument("--refresh", action="store_true", help="deliberately replace a frozen snapshot")
    parser.add_argument("--skip-spy", action="store_true", help="skip the recent yfinance SPY minute snapshot")
    args = parser.parse_args()
    try:
        collect(
            args.date, data_dir=args.data_dir, strike_range=args.strike_range,
            refresh=args.refresh, include_spy=not args.skip_spy,
        )
        return 0
    except Exception as exc:
        print(f"Collection failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
