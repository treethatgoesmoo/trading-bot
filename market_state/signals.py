"""Pure, versioned completed-candle SPX state observations.

5m is the decision timeframe, completed 15m is causal directional context,
1m is display context only. Two consecutive CURRENT-session 5m observations
must agree to establish direction; disagreement is neutral, not volatility.
Volatility is independent and clears after two quiet 5m observations.

Two complete immediately preceding regular sessions may warm indicators. Bars
never bridge sessions, but indicator series and true ranges include overnight
changes. Receipts gate what is knowable at computation time. Retrospective
confirmation describes completed prices, NOT a claim of an earlier delivery.
35 bars is a warm-up convention, not proof of EMA convergence. Volatility needs
40 bars (20 prior valid BB widths); confirmation therefore needs 41 in total.
The caller owns transition detection, delivery freshness, and persistence.
"""
from __future__ import annotations

from datetime import date, timedelta
import math
from statistics import median

import pandas as pd

from core import market_calendar
from core.indicators import bollinger, macd, rsi_series
from core.session_bars import session_time, utc, valid_bar

VERSION = 'spx-state-v1'
TIMEFRAMES = ('1', '5', '15')
MIN_HISTORY = 35
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BULLISH_RSI = 55.0
BEARISH_RSI = 45.0
CONTEXT_RSI = 50.0
VOLATILITY_LOOKBACK = 20
VOLATILITY_MULTIPLIER = 1.5
VOLATILITY_MIN_HISTORY = BB_PERIOD + VOLATILITY_LOOKBACK
_MINUTE = timedelta(minutes=1)


def preceding_sessions(day):
    """Return the two immediately preceding trading dates, newest first."""
    days = []
    while len(days) < 2:
        day -= timedelta(days=1)
        if market_calendar.is_trading_day(day):
            days.append(day)
    return days


def _empty(reason):
    return dict(available=False, reason=reason, bar_at=None, close=None,
                upper=None, middle=None, lower=None, rsi=None, rsi_change=None,
                macd_histogram=None, macd_change=None, band_width=None,
                state='unavailable')


def _empty_classification(reason, bar_at=None):
    return dict(available=False, reason=reason, bar_at=bar_at,
                direction='unavailable', volatile=None, reasons=[],
                raw_current=None, raw_previous=None)


def _rows(bars, day, now):
    """Filter future receipts/unfinished bars; reject bad available evidence."""
    rows = {}
    for bar in bars:
        stamp, available = utc(bar['timestamp']), utc(bar['available_at'])
        if stamp + _MINUTE > now or available > now:
            continue
        if available < stamp + _MINUTE or not valid_bar(bar, day, now) or stamp in rows:
            raise ValueError('Invalid or duplicate completed minute')
        rows[stamp] = dict(timestamp=stamp,
                           **{k: float(bar[k]) for k in ('open', 'high', 'low', 'close')})
    return [rows[t] for t in sorted(rows)]


def _contiguous(rows, opening, end):
    count = int((end - opening).total_seconds() / 60)
    return len(rows) == count and all(row['timestamp'] == opening + i * _MINUTE
                                     for i, row in enumerate(rows))


def _buckets(rows, day, minutes):
    opening = session_time(day, 9, 30)
    groups = {}
    for row in rows:
        index = int((row['timestamp'] - opening).total_seconds() // (minutes * 60))
        groups.setdefault(index, []).append(row)
    result = []
    for index in sorted(groups):
        group = groups[index]
        start = opening + timedelta(minutes=index * minutes)
        if _contiguous(group, start, start + timedelta(minutes=minutes)):
            result.append(dict(timestamp=start, open=group[0]['open'],
                               high=max(r['high'] for r in group),
                               low=min(r['low'] for r in group), close=group[-1]['close']))
    return result


def _finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


def _series(rows):
    closes = pd.Series([r['close'] for r in rows], dtype=float)
    upper, middle, lower = bollinger(closes, BB_PERIOD, BB_STD)
    rsi = rsi_series(closes, RSI_PERIOD)
    _, _, histogram = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    return upper, middle, lower, rsi, histogram


def _frame(rows):
    if len(rows) < MIN_HISTORY:
        return dict(_empty('warming_up'),
                    bar_at=rows[-1]['timestamp'].isoformat() if rows else None,
                    history_bars=len(rows), required_bars=MIN_HISTORY)
    upper, middle, lower, rsi, histogram = _series(rows)
    if not all(math.isfinite(float(value)) for series in (upper, middle, lower, histogram)
               for value in series.iloc[-2:]):
        return _empty('invalid_indicator_values')
    cur = rows[-1]
    up, mid, lo = (float(series.iloc[-1]) for series in (upper, middle, lower))
    if mid <= 0 or up < lo:
        return _empty('invalid_indicator_values')
    width = (up - lo) / mid
    rsi_now, rsi_previous = _finite(rsi.iloc[-1]), _finite(rsi.iloc[-2])
    change = None if rsi_now is None or rsi_previous is None else rsi_now - rsi_previous
    hist_change = float(histogram.iloc[-1] - histogram.iloc[-2])
    if not math.isfinite(width) or not math.isfinite(hist_change):
        return _empty('invalid_indicator_values')
    return dict(available=True, reason='complete', bar_at=cur['timestamp'].isoformat(),
                close=cur['close'], upper=up, middle=mid, lower=lo,
                rsi=rsi_now, rsi_change=change, macd_histogram=float(histogram.iloc[-1]),
                macd_change=hist_change, band_width=width,
                state=('flat_band' if up == lo else 'above_upper' if cur['close'] > up else
                       'below_lower' if cur['close'] < lo else 'inside_or_on_band'))


def _raw_direction(five, fifteen):
    """No weighted votes. A disagreement is neutral, missing input unavailable."""
    for frame in (five, fifteen):
        if not frame.get('available'):
            return 'unavailable'
        if any(frame.get(k) is None or not math.isfinite(float(frame[k]))
               for k in ('close', 'middle', 'rsi', 'macd_histogram')):
            return 'unavailable'
    if (five['close'] > five['middle'] and five['rsi'] >= BULLISH_RSI
            and five['macd_histogram'] > 0 and fifteen['close'] > fifteen['middle']
            and fifteen['rsi'] >= CONTEXT_RSI and fifteen['macd_histogram'] >= 0):
        return 'bullish'
    if (five['close'] < five['middle'] and five['rsi'] <= BEARISH_RSI
            and five['macd_histogram'] < 0 and fifteen['close'] < fifteen['middle']
            and fifteen['rsi'] <= CONTEXT_RSI and fifteen['macd_histogram'] <= 0):
        return 'bearish'
    return 'neutral'


def _volatility_test(true_range, range_baseline, width, width_baseline, previous_width):
    """Evaluate two independent movement measures without inventing zero ratios."""
    values = (true_range, range_baseline, width, width_baseline, previous_width)
    base = dict(available=False, reason='invalid_volatility_values', trigger=None,
                true_range=true_range, range_baseline=range_baseline,
                band_width=width, width_baseline=width_baseline,
                previous_width=previous_width, range_trigger=None, width_trigger=None)
    if any(value is None or not math.isfinite(float(value)) or value < 0 for value in values):
        # Never retain caller NaN/Infinity in a JSON result.
        for key in ('true_range', 'range_baseline', 'band_width', 'width_baseline', 'previous_width'):
            value = base[key]
            base[key] = _finite(value) if value is not None else None
        return base
    if (range_baseline == 0 and true_range > 0) or (width_baseline == 0 and width > 0):
        base['reason'] = 'zero_volatility_baseline'
        return base
    # 0 >= 1.5 * 0 must not fabricate volatility on a flat baseline.
    range_trigger = range_baseline > 0 and true_range >= VOLATILITY_MULTIPLIER * range_baseline
    width_trigger = (width_baseline > 0 and width >= VOLATILITY_MULTIPLIER * width_baseline
                     and width > previous_width)
    return dict(base, available=True, reason='complete',
                trigger=bool(range_trigger or width_trigger),
                range_trigger=bool(range_trigger), width_trigger=bool(width_trigger))


def _raw_volatility(rows):
    if len(rows) < VOLATILITY_MIN_HISTORY:
        return dict(available=False, reason='volatility_warming_up', trigger=None,
                    history_bars=len(rows), required_bars=VOLATILITY_MIN_HISTORY)
    upper, middle, lower, _, _ = _series(rows)
    widths = ((upper - lower) / middle).iloc[-(VOLATILITY_LOOKBACK + 1):]
    if any(not math.isfinite(float(value)) or value < 0 for value in widths):
        return dict(available=False, reason='invalid_volatility_values', trigger=None)
    ranges = []
    for i in range(len(rows) - VOLATILITY_LOOKBACK - 1, len(rows)):
        row, previous = rows[i], rows[i - 1]
        ranges.append(max(row['high'] - row['low'], abs(row['high'] - previous['close']),
                          abs(row['low'] - previous['close'])))
    # Both baselines exclude the current candle. The prior close may be in the
    # preceding session; overnight price changes deliberately contribute to TR.
    return _volatility_test(ranges[-1], median(ranges[:-1]), float(widths.iloc[-1]),
                            median(float(v) for v in widths.iloc[:-1]), float(widths.iloc[-2]))


def _classification(current_five, current_fifteen, prior_five, prior_fifteen):
    stamp = current_five[-1]['timestamp'].isoformat() if current_five else None
    if len(current_five) < 2:
        return _empty_classification('need_two_current_session_5m_candles', stamp)
    raw = []
    for count in (len(current_five) - 1, len(current_five)):
        five_rows = prior_five + current_five[:count]
        end = current_five[count - 1]['timestamp'] + timedelta(minutes=5)
        # Require current-session 15m context. Yesterday's final15m must not
        # provide a fresh directional gate before today's first15m close.
        completed_fifteen = [row for row in current_fifteen
                             if row['timestamp'] + timedelta(minutes=15) <= end]
        five = _frame(five_rows)
        fifteen = (_frame(prior_fifteen + completed_fifteen) if completed_fifteen
                   else _empty('no_contemporaneous_15m_candle'))
        direction = _raw_direction(five, fifteen)
        volatility = _raw_volatility(five_rows)
        raw.append(dict(bar_at=current_five[count - 1]['timestamp'].isoformat(),
                        direction=direction, context_bar_at=fifteen['bar_at'],
                        five=five, fifteen=fifteen, volatility=volatility))
    previous, current = raw
    base = dict(_empty_classification('indicator_unavailable', stamp),
                raw_current=current, raw_previous=previous)
    if any(entry['direction'] == 'unavailable' for entry in raw):
        return base
    if any(not entry['volatility']['available'] for entry in raw):
        return dict(base, reason='volatility_unavailable')
    direction = (current['direction'] if current['direction'] == previous['direction']
                 and current['direction'] in ('bullish', 'bearish') else 'neutral')
    volatile = current['volatility']['trigger'] or previous['volatility']['trigger']
    reasons = ([f'Two completed 5m candles meet {direction} price/RSI/MACD conditions '
                'with their completed 15m context.'] if direction != 'neutral' else
               ['No persistent aligned direction across two completed 5m candles.'])
    if volatile:
        if current['volatility']['range_trigger']:
            reasons.append('5m true range is at least 1.5× its preceding 20-candle median.')
        if current['volatility']['width_trigger']:
            reasons.append('5m BB width is expanding and at least 1.5× its preceding 20-candle median.')
        if not current['volatility']['trigger']:
            reasons.append('Volatility remains elevated until two consecutive quiet 5m candles.')
    else:
        reasons.append('Neither of the last two 5m candles triggered elevated volatility.')
    return dict(base, available=True, reason='complete', direction=direction,
                volatile=bool(volatile), reasons=reasons)


def build_snapshot(record, previous_bars, now):
    """Build JSON-safe observations without I/O or mutating caller evidence.

    `bar_at` is latest completed MINUTE START. `classification.bar_at` is the
    latest completed 5m START, and can remain older than 90s in a fresh snapshot:
    the delivery layer MUST separately gate that 5m candle's end age. Display
    context is not an eligible alert and this function makes no delivery claim.
    """
    now = utc(now)
    snapshot = dict(version=VERSION, session_date=record.get('session_date'), as_of=now.isoformat(),
                    bar_at=None, fresh=False, reason='invalid_data', prior_complete=False, prior_days=[],
                    timeframes={tf: _empty('invalid_data') for tf in TIMEFRAMES},
                    classification=_empty_classification('invalid_data'))

    def unavailable(reason):
        snapshot['reason'] = reason
        snapshot['timeframes'] = {tf: _empty(reason) for tf in TIMEFRAMES}
        snapshot['classification'] = _empty_classification(reason)
        return snapshot

    try:
        day = date.fromisoformat(record['session_date'])
        opening, closing = session_time(day, 9, 30), session_time(day, *market_calendar.close_time(day))
        if (now.astimezone(market_calendar.ET).date() != day or
                not market_calendar.is_trading_day(day) or not opening <= now <= closing):
            return unavailable('market_closed')
        current = _rows(record['bars'], day, now)
        if not current:
            return unavailable('no_completed_bars')
        latest_end = current[-1]['timestamp'] + _MINUTE
        snapshot['bar_at'] = current[-1]['timestamp'].isoformat()
        if not _contiguous(current, opening, latest_end):
            return unavailable('incomplete_session')
        if not 0 <= (now - latest_end).total_seconds() <= 90:
            return unavailable('stale_data')
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unavailable('invalid_data')

    grouped, prior = {}, {}
    try:
        for bar in previous_bars:
            prior_day = utc(bar['timestamp']).astimezone(market_calendar.ET).date()
            grouped.setdefault(prior_day, []).append(bar)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        grouped = {}
    for prior_day in preceding_sessions(day):
        try:
            rows = _rows(grouped.get(prior_day, []), prior_day, now)
            if not _contiguous(rows, session_time(prior_day, 9, 30),
                               session_time(prior_day, *market_calendar.close_time(prior_day))):
                break
            prior[prior_day] = rows
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            break
    snapshot.update(fresh=True, reason='complete', prior_complete=bool(prior),
                    prior_days=[d.isoformat() for d in sorted(prior)])
    current_buckets, prior_buckets = {}, {}
    for tf in TIMEFRAMES:
        minutes = int(tf)
        current_buckets[tf] = _buckets(current, day, minutes)
        prior_buckets[tf] = [bar for d in sorted(prior) for bar in _buckets(prior[d], d, minutes)]
        snapshot['timeframes'][tf] = (
            _frame(prior_buckets[tf] + current_buckets[tf]) if current_buckets[tf]
            else _empty('no_completed_candle'))
    snapshot['classification'] = _classification(current_buckets['5'], current_buckets['15'],
                                                  prior_buckets['5'], prior_buckets['15'])
    return snapshot
