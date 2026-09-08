"""Neutral, first-seen completed-minute storage. No strategy or experiment hooks."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math

from core import market_calendar

KIND = 'session-bars-v1'
_MINUTE = timedelta(minutes=1)


def utc(value: datetime | str) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timezone-aware timestamp required')
    return value.astimezone(timezone.utc)


def session_time(day: date, hour: int, minute: int = 0) -> datetime:
    return market_calendar.ET.localize(datetime(day.year, day.month, day.day, hour, minute)).astimezone(timezone.utc)


def new_record(day: date) -> dict:
    return dict(schema=1, kind=KIND, session_date=day.isoformat(), bars=[],
                polls=0, fetch_failures=0, rejected_bars=0)


def valid_bar(bar: dict, day: date, received_at: datetime) -> bool:
    try:
        stamp = utc(bar['timestamp'])
        if any(isinstance(bar[k], bool) for k in ('open', 'high', 'low', 'close')):
            return False
        o, h, l, c = (float(bar[k]) for k in ('open', 'high', 'low', 'close'))
        return (market_calendar.is_trading_day(day) and stamp.second == stamp.microsecond == 0
                and session_time(day, 9, 30) <= stamp < session_time(day, *market_calendar.close_time(day))
                and stamp + _MINUTE <= utc(received_at)
                and all(math.isfinite(v) and v > 0 for v in (o, h, l, c))
                and l <= min(o, c) <= max(o, c) <= h)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def validate_record(record: dict, day: date) -> None:
    """Validate storage, not continuity; gaps must remain visible to the engine."""
    if (not isinstance(record, dict) or type(record.get('schema')) is not int
            or record['schema'] != 1 or record.get('kind') != KIND
            or record.get('session_date') != day.isoformat()
            or not isinstance(record.get('bars'), list)
            or any(k in record for k in ('candidate', 'outcome', 'rule', 'paper_only'))):
        raise ValueError('Invalid neutral session record')
    for key in ('polls', 'fetch_failures', 'rejected_bars'):
        if type(record.get(key)) is not int or record[key] < 0:
            raise ValueError('Invalid session counter')
    previous = None
    for bar in record['bars']:
        stamp, available = utc(bar['timestamp']), utc(bar['available_at'])
        if (not valid_bar(bar, day, available) or (previous is not None and stamp <= previous)
                or bar['timestamp'] != stamp.isoformat() or bar['available_at'] != available.isoformat()):
            raise ValueError('Invalid persisted minute or first-seen receipt')
        previous = stamp
    for key in ('last_poll_at', 'last_success_at'):
        if record.get(key) is not None:
            utc(record[key])


def update_record(record: dict, bars, received_at: datetime, *, data_cutoff: datetime | None = None) -> dict:
    """Ingest only complete requested minutes, preserving original receipts/prices.

    Historical-session requests retain their actual (later) receipt time. An
    incoming duplicate timestamp invalidates that timestamp within the response,
    including when one copy has invalid prices. Already stored bars never change.
    No candidate discovery, grading, inferred volume, or forward filling occurs.
    """
    received_at = utc(received_at)
    cutoff = min(received_at, utc(data_cutoff)) if data_cutoff is not None else received_at
    day = date.fromisoformat(record['session_date'])
    validate_record(record, day)
    known = {utc(b['timestamp']): b for b in record['bars']}
    incoming, seen, duplicates = {}, set(), set()
    rejected = 0
    # Do not partially mutate the record if iterating a provider response fails.
    for bar in bars:
        try:
            row = dict(bar) if isinstance(bar, dict) else {
                key: getattr(bar, key) for key in ('timestamp', 'open', 'high', 'low', 'close')}
            stamp = utc(row['timestamp'])
            if stamp in seen:
                duplicates.add(stamp)
            seen.add(stamp)
            if not valid_bar(row, day, cutoff):
                raise ValueError('Invalid or incomplete minute')
            incoming[stamp] = dict(timestamp=stamp.isoformat(), available_at=received_at.isoformat(),
                                   **{k: float(row[k]) for k in ('open', 'high', 'low', 'close')})
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            rejected += 1
    for stamp, row in incoming.items():
        if stamp not in duplicates:
            known.setdefault(stamp, row)
    record['bars'] = [known[t] for t in sorted(known)]
    record['rejected_bars'] += rejected + len(duplicates)
    record['last_success_at'] = received_at.isoformat()
    return record
