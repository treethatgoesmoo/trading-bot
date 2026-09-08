"""Optional Yahoo SPY context, not consolidated or executable SPX data."""
from datetime import timedelta
import math

from core.market_calendar import ET
from zero_dte.orb_audit import session_time, utc


def summarize_proxy(frame, now):
    now = utc(now)
    day = now.astimezone(ET).date()
    missing = {"status": "unavailable", "source": "Yahoo SPY indicative"}
    try:
        rows = {}
        for stamp, row in frame.iterrows():
            t = utc(stamp.to_pydatetime())
            if t.astimezone(ET).date() != day or t < session_time(day, 9, 30):
                continue
            if t + timedelta(minutes=1) > now:
                continue
            if t.second or t.microsecond or t in rows:
                return missing
            values = [float(row[k]) for k in ('High', 'Low', 'Close', 'Volume')]
            h, l, c, volume = values
            if not all(math.isfinite(v) for v in values) or not 0 < l <= c <= h or volume < 0:
                return missing
            rows[t] = values
        if not rows:
            return missing
        latest = max(rows)
        if not 0 <= (now - latest - timedelta(minutes=1)).total_seconds() <= 90:
            return {**missing, "status": "stale"}
        stamp = session_time(day, 9, 30)
        ordered = []
        while stamp <= latest:
            if stamp not in rows:
                return missing
            ordered.append(rows[stamp])
            stamp += timedelta(minutes=1)
        blocks = len(ordered) // 5
        if blocks < 4:
            return missing
        total_volume = sum(r[3] for r in ordered)
        completed = ordered[:blocks * 5]
        preceding = sum(r[3] for r in completed[-20:-5]) / 3
        if total_volume <= 0 or preceding <= 0:
            return missing
        vwap = sum((r[0] + r[1] + r[2]) / 3 * r[3] for r in ordered) / total_volume
        return {"status": "available", "source": "Yahoo SPY indicative",
                "above_vwap": ordered[-1][2] > vwap,
                "volume_ratio": sum(r[3] for r in completed[-5:]) / preceding,
                "bar_at": latest.isoformat(),
                "baseline": "last completed 5m / mean of preceding three completed 5m"}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return missing


def fetch_proxy(now):
    """Synchronous; callers must run in a worker. No fallback or Theta calls."""
    import yfinance as yf
    frame = yf.Ticker('SPY').history(period='1d', interval='1m', prepost=False)
    return summarize_proxy(frame, now)
