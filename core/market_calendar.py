"""NYSE trading calendar — rule-based holidays and early closes (no extra deps).

Standard NYSE full-day holidays: New Year's Day, MLK Day, Washington's
Birthday, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day,
Thanksgiving, Christmas. Date-fixed holidays falling on a Saturday are
observed the Friday before (except New Year's, which NYSE does not move into
the prior year) and on a Sunday the Monday after.

Early closes (1:00 PM ET): the trading day before Independence Day's
observed holiday, the day after Thanksgiving, and Christmas Eve when it is a
trading day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytz

ET = pytz.timezone("US/Eastern")

REGULAR_CLOSE = (16, 0)
EARLY_CLOSE = (13, 0)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, 12, 31) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, saturday_to_friday: bool = True) -> date | None:
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> set[date]:
    hs: set[date] = set()
    new_year = _observed(date(year, 1, 1), saturday_to_friday=False)
    if new_year:
        hs.add(new_year)
    hs.add(_nth_weekday(year, 1, 0, 3))            # MLK Day — 3rd Monday of January
    hs.add(_nth_weekday(year, 2, 0, 3))            # Washington's Birthday — 3rd Monday of February
    hs.add(_easter(year) - timedelta(days=2))      # Good Friday
    hs.add(_last_weekday(year, 5, 0))              # Memorial Day — last Monday of May
    for month, day in ((6, 19), (7, 4), (12, 25)):  # Juneteenth, Independence Day, Christmas
        if (month, day) == (6, 19) and year < 2022:
            continue  # NYSE only observes Juneteenth from 2022 — matters for deep backtests
        obs = _observed(date(year, month, day))
        if obs:
            hs.add(obs)
    hs.add(_nth_weekday(year, 9, 0, 1))            # Labor Day — 1st Monday of September
    hs.add(_nth_weekday(year, 11, 3, 4))           # Thanksgiving — 4th Thursday of November
    return hs


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def early_closes(year: int) -> set[date]:
    ec: set[date] = set()
    independence_observed = _observed(date(year, 7, 4))
    if independence_observed:
        # The session immediately preceding the observed Independence Day
        # holiday closes early. This covers July 2 when July 4 is a Sunday
        # (e.g. 2027), as well as the usual July 3 case.
        july_early = independence_observed - timedelta(days=1)
        while not is_trading_day(july_early):
            july_early -= timedelta(days=1)
        ec.add(july_early)
    ec.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    dec24 = date(year, 12, 24)
    if is_trading_day(dec24):
        ec.add(dec24)
    return ec


def close_time(d: date) -> tuple[int, int]:
    """(hour, minute) of the session close in ET — (13, 0) on early-close days."""
    return EARLY_CLOSE if d in early_closes(d.year) else REGULAR_CLOSE


def session_close(now: datetime) -> datetime:
    """The session-close datetime for `now`'s date (same tzinfo as `now`)."""
    hour, minute = close_time(now.date())
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    if not is_trading_day(now.date()):
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return market_open <= now <= session_close(now)
