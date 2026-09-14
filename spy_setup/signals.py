"""Pure SPY opening-range/retest setup replay, not SPX fills or a proven edge.

All prices are SPY. Minute timestamps denote starts; decisions happen at closes.
Thresholds are explicit initial conventions, not optimized estimates. History is
receipt-gated at each decision, so later downloads cannot create earlier setups.
The caller owns persistence, transition deduplication and delivery eligibility.
"""
from __future__ import annotations

from datetime import date, timedelta
import copy
import math
from statistics import median

from core import market_calendar
from core.session_bars import session_time, utc, valid_bar

VERSION = 'spy-orb-reversal-v2'
OPENING_MINUTES = 15
HISTORY_SESSIONS = 5
MIN_VOLUME_SAMPLES = 3
MIN_RELATIVE_VOLUME = 1.2
PRICE_BUFFER = 0.01
RETEST_TOLERANCE_FRACTION = 0.10
PENDING_MINUTES = 15
TARGET_R = 2.0
FRESH_SECONDS = 90
_MINUTE = timedelta(minutes=1)
TERMINAL = ('invalidated', 'expired', 'target_reached')


def preceding_sessions(day):
    days = []
    while len(days) < HISTORY_SESSIONS:
        day -= timedelta(days=1)
        if market_calendar.is_trading_day(day):
            days.append(day)
    return days


def state_key(classification):
    """Lifecycle identity; direction alone MUST NOT drive delivery transitions."""
    if not classification.get('available'):
        raise ValueError('Unavailable setup classification')
    return {key: classification[key] for key in ('setup_id', 'lifecycle', 'phase', 'direction')}


def _unavailable(reason):
    return dict(available=False, reason=reason, bar_at=None, direction='unavailable',
                reasons=[], setup_id=None, lifecycle='unavailable', phase=None)


def _rows(bars, day, now):
    rows = {}
    for bar in bars:
        stamp, receipt = utc(bar['timestamp']), utc(bar['available_at'])
        if receipt > now or stamp + _MINUTE > now:
            continue
        volume = bar.get('volume')
        if (not valid_bar(bar, day, now) or receipt < stamp + _MINUTE or stamp in rows
                or isinstance(volume, bool) or volume is None
                or not math.isfinite(float(volume)) or float(volume) < 0):
            raise ValueError('Invalid completed OHLCV minute')
        rows[stamp] = dict(timestamp=stamp, available_at=receipt,
                           **{key: float(bar[key]) for key in ('open', 'high', 'low', 'close', 'volume')})
    return [rows[key] for key in sorted(rows)]


def _baseline(history, stamp, decision_at):
    slot = stamp.astimezone(market_calendar.ET).strftime('%H:%M')
    samples = [row['volume'] for rows in history.values() for row in rows
               if row['timestamp'].astimezone(market_calendar.ET).strftime('%H:%M') == slot
               and row['available_at'] <= decision_at and row['volume'] > 0]
    return (median(samples) if len(samples) >= MIN_VOLUME_SAMPLES else None), len(samples)


def build_snapshot(record, previous_bars, now):
    """Return JSON-safe current classification plus chronological lifecycle events.

    classification.bar_at is latest evaluated 1m START (not the transition time).
    event_at is transition candle CLOSE or the exact scheduled expiry deadline;
    it never advances on unchanged minutes. Stale expiry is informational only.
    A sender must require fresh=True, available=True, setup_id != None and
    0 <= now-event_at <=90s, then deduplicate state_key. No backfill notifications.
    timeframes contains only '1'; consumers must retire their 5m assumptions.
    """
    now = utc(now)
    snapshot = dict(version=VERSION, signal_symbol='SPY', execution_symbol='SPX',
                    session_date=record.get('session_date'), as_of=now.isoformat(),
                    bar_at=None, fresh=False, reason='invalid_data', prior_complete=False,
                    prior_days=[], timeframes={}, classification=_unavailable('invalid_data'), events=[])

    def unavailable(reason):
        snapshot.update(reason=reason, classification=_unavailable(reason))
        return snapshot

    try:
        day = date.fromisoformat(record['session_date'])
        opening = session_time(day, 9, 30)
        closing = session_time(day, *market_calendar.close_time(day))
        if (now.astimezone(market_calendar.ET).date() != day or
                not market_calendar.is_trading_day(day) or not opening <= now < closing):
            return unavailable('market_closed')
        rows = _rows(record['bars'], day, now)
        if not rows:
            return unavailable('no_completed_bars')
        snapshot['bar_at'] = rows[-1]['timestamp'].isoformat()
        if any(row['timestamp'] != opening + i * _MINUTE for i, row in enumerate(rows)):
            return unavailable('incomplete_session')
        data_fresh = 0 <= (now - rows[-1]['timestamp'] - _MINUTE).total_seconds() <= FRESH_SECONDS
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unavailable('invalid_data')

    history, grouped = {}, {}
    try:
        for bar in previous_bars:
            prior_day = utc(bar['timestamp']).astimezone(market_calendar.ET).date()
            grouped.setdefault(prior_day, []).append(bar)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unavailable('invalid_history')
    for prior_day in preceding_sessions(day):
        try:
            history[prior_day] = _rows(grouped.get(prior_day, []), prior_day, now)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            # A corrupt session is excluded, not partially counted as a baseline.
            history[prior_day] = []
    snapshot.update(fresh=data_fresh, reason='complete' if data_fresh else 'stale_data',
                    prior_days=[d.isoformat() for d, values in history.items() if values],
                    prior_complete=all(
                        len(values) == int((session_time(d, *market_calendar.close_time(d))
                                            - session_time(d, 9, 30)).total_seconds() / 60)
                        and all(row['timestamp'] == session_time(d, 9, 30) + i * _MINUTE
                                for i, row in enumerate(values))
                        for d, values in history.items()))
    latest = rows[-1]
    snapshot['timeframes'] = {'1': dict(available=True, bar_at=snapshot['bar_at'], close=latest['close'])}
    if len(rows) < OPENING_MINUTES:
        return unavailable('opening_range_forming')

    high = max(row['high'] for row in rows[:OPENING_MINUTES])
    low = min(row['low'] for row in rows[:OPENING_MINUTES])
    width = high - low
    snapshot['opening_range'] = dict(high=high, low=low)
    if width <= 0:
        return unavailable('invalid_opening_range')
    tolerance = width * RETEST_TOLERANCE_FRACTION
    cutoff = min(session_time(day, 12), closing - timedelta(minutes=15))
    forced_exit = closing - timedelta(minutes=5)
    setup, used, volume_sum, typical_sum = None, set(), 0.0, 0.0
    latest_vwap, latest_rvol, latest_samples = None, None, 0
    breakdown_at = None

    def transition(lifecycle, phase, row, reason, *, at=None):
        setup.update(lifecycle=lifecycle, phase=phase, event_at=(at or row['timestamp'] + _MINUTE).isoformat(),
                     reasons=[reason])
        snapshot['events'].append(copy.deepcopy(setup))

    for index, row in enumerate(rows):
        volume_sum += row['volume']
        typical_sum += (row['high'] + row['low'] + row['close']) / 3 * row['volume']
        vwap = typical_sum / volume_sum if volume_sum > 0 else None
        end = row['timestamp'] + _MINUTE
        # Use actual knowability, but never later than freshness permits.
        decision_at = max(end, row['available_at'])
        actionable = ((decision_at - end).total_seconds() <= FRESH_SECONDS
                      and all(prior['available_at'] <= decision_at for prior in rows[:index]))
        baseline, samples = _baseline(history, row['timestamp'], decision_at)
        rvol = row['volume'] / baseline if baseline else None
        latest_vwap, latest_rvol, latest_samples = vwap, rvol, samples
        if index < OPENING_MINUTES:
            continue
        # Arm only from an observed, timely completed downside break. This
        # contextual fact is not an entry and needs no volume/VWAP vote.
        if actionable and row['close'] <= low - PRICE_BUFFER:
            breakdown_at = end
        if setup and setup['lifecycle'] not in TERMINAL:
            bullish = setup['direction'] == 'bullish'
            stop_hit = row['low'] <= setup['invalidation'] if bullish else row['high'] >= setup['invalidation']
            deadline = utc(setup['expires_at'])
            if end > deadline:
                transition('expired', 'terminal', row, 'Scheduled expiry; no price outcome or option exit assumed.', at=deadline)
            elif stop_hit:
                transition('invalidated', 'terminal', row, 'SPY invalidation touched; stop takes priority over target on ambiguous candles.')
            elif setup['lifecycle'] == 'triggered':
                target_hit = row['high'] >= setup['target'] if bullish else row['low'] <= setup['target']
                if target_hit:
                    transition('target_reached', 'terminal', row, 'SPY 2R reference target touched; not an SPX option fill or return.')
                elif end >= deadline:
                    transition('expired', 'terminal', row, 'Session safety expiry five minutes before close; no option exit assumed.', at=deadline)
            elif end >= utc(setup['expires_at']) or end >= cutoff:
                transition('expired', 'terminal', row, 'Pending setup expired; no new entry.')
            elif setup['phase'] == 'breakout':
                level = setup['boundary']
                held = (row['low'] <= level + tolerance and row['close'] >= level + PRICE_BUFFER if bullish
                        else row['high'] >= level - tolerance and row['close'] <= level - PRICE_BUFFER)
                aligned = vwap is not None and (row['close'] > vwap if bullish else row['close'] < vwap)
                if actionable and held and aligned:
                    setup['trigger'] = row['high'] + PRICE_BUFFER if bullish else row['low'] - PRICE_BUFFER
                    setup['invalidation'] = row['low'] - PRICE_BUFFER if bullish else row['high'] + PRICE_BUFFER
                    transition('forming', 'retest', row, 'Retest held with VWAP alignment; await a later completed continuation candle.')
            elif setup['phase'] == 'retest':
                crossed = row['close'] >= setup['trigger'] if bullish else row['close'] <= setup['trigger']
                aligned = vwap is not None and (row['close'] > vwap if bullish else row['close'] < vwap)
                if actionable and crossed and aligned and rvol is not None and rvol >= MIN_RELATIVE_VOLUME:
                    setup['entry_reference'] = row['close']
                    risk = abs(row['close'] - setup['invalidation'])
                    setup['target'] = row['close'] + (1 if bullish else -1) * TARGET_R * risk
                    setup['expires_at'] = forced_exit.isoformat()
                    transition('triggered', 'active', row, 'Completed SPY continuation confirmed with VWAP and relative volume; reference entry is this close, not the earlier trigger level.')
            # Never create another setup on the same bar as a lifecycle decision.
            continue
        # Late historical current bars can provide context but cannot originate
        # actionable setups on replay. Current history availability is causal.
        if (end >= cutoff or not actionable
                or vwap is None or rvol is None or rvol < MIN_RELATIVE_VOLUME):
            continue
        direction = ('bullish' if row['close'] >= high + PRICE_BUFFER and row['close'] > vwap else
                     'bearish' if row['close'] <= low - PRICE_BUFFER and row['close'] < vwap else None)
        # A new breakout requires crossing the opening boundary from inside,
        # not a first qualifying high-volume candle far into an existing trend.
        if direction == 'bullish' and rows[index - 1]['close'] > high:
            direction = None
        if direction == 'bearish' and rows[index - 1]['close'] < low:
            direction = None
        kind = 'opening_range_breakout'
        # Prefer an ordinary qualifying ORB; otherwise a previous downside
        # break can qualify a bullish reclaim, including after put invalidation.
        if (direction is None and breakdown_at is not None and breakdown_at < end
                and row['close'] >= low + PRICE_BUFFER and row['close'] > vwap):
            direction, kind = 'bullish', 'failed_breakdown_reversal'
        if direction is None or direction in used:
            continue
        boundary = low if kind == 'failed_breakdown_reversal' or direction == 'bearish' else high
        used.add(direction)
        setup = dict(setup_id=f'{VERSION}/{day}/{direction}/{end.isoformat()}', direction=direction,
                     kind=kind, boundary=boundary,
                     breakdown_at=breakdown_at.isoformat() if kind == 'failed_breakdown_reversal' else None,
                     option_side='CALL' if direction == 'bullish' else 'PUT',
                     breakout_at=end.isoformat(), expires_at=min(end + timedelta(minutes=PENDING_MINUTES), cutoff).isoformat(),
                     trigger=None, invalidation=boundary - tolerance if direction == 'bullish' else boundary + tolerance,
                     entry_reference=None, target=None, breakout_relative_volume=rvol)
        reason = ('Failed-breakdown bullish reclaim confirmed above the opening-range low with VWAP alignment and at least 1.2x same-minute median volume; wait for a later holding retest, not an entry.'
                  if kind == 'failed_breakdown_reversal' else
                  'Opening-range breakout confirmed with VWAP alignment and at least 1.2x same-minute median volume; wait for retest, do not chase.')
        transition('forming', 'breakout', row, reason)

    # Time alone can retire a setup, even without a new candle. Never synthesize
    # a candle/price or move the event forward to the next receipt time.
    if setup and setup['lifecycle'] not in TERMINAL and now >= utc(setup['expires_at']):
        transition('expired', 'terminal', latest,
                   'Scheduled expiry; no price outcome or option exit assumed.', at=utc(setup['expires_at']))
    snapshot.update(vwap=latest_vwap, relative_volume=latest_rvol, volume_samples=latest_samples)
    classification = dict(available=True, reason='complete', bar_at=snapshot['bar_at'],
                          direction='neutral', setup_id=None, lifecycle='idle', phase=None,
                          event_at=None, reasons=['No qualifying setup.'])
    if setup:
        classification.update(setup)
    elif latest_rvol is None:
        return unavailable('volume_baseline_unavailable')
    if not data_fresh:
        # Retain the clock-expiry fact for inspection but not actionable levels
        # or a directional classification. Existing store/delivery contracts
        # reject unavailable snapshots as observations and notifications.
        expired = setup and setup['lifecycle'] == 'expired'
        unavailable('stale_data')
        if expired:
            snapshot['classification'].update(lifecycle='expired', phase='terminal',
                                               event_at=setup['event_at'], expires_at=setup['expires_at'])
        return snapshot
    snapshot['classification'] = classification
    return snapshot
