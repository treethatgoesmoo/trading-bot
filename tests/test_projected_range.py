"""Offline synthetic range rules and isolated-client contracts."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core.session_bars import new_record, session_time, update_record
from spy_setup.projected_range import RangeResult, SAMPLE_SIZE, _close, _sessions, range_session as calculate_range

DAY = date(2026, 9, 15)
NOW = session_time(DAY, 10, 0) + timedelta(seconds=20)


def range_session(day, now, **kwargs):
    """Synthetic receipt clock defaults to this test's supplied cutoff."""
    kwargs.setdefault('clock', lambda: now)
    return calculate_range(day, now, **kwargs)


def rows(day, *, minutes=None, spread=1, anchor=100):
    start = session_time(day, 9, 30)
    minutes = minutes if minutes is not None else int((_close(day) - start).total_seconds() / 60)
    return [dict(timestamp=start + timedelta(minutes=i), open=anchor,
                 high=anchor + spread, low=anchor - spread, close=anchor, volume=100)
            for i in range(minutes)]


def cache(day=DAY, now=NOW):
    result = {}
    for i, session in enumerate(_sessions(day)):
        record = new_record(session)
        update_record(record, rows(session, spread=i + 1), now)
        result[session] = record
    return result


class Client:
    def __init__(self, responses=None, failure=None):
        self.responses = responses or {}
        self.failure = failure
        self.calls = []
        self.closed = False
        self.purpose = None

    def factory(self, **kwargs):
        self.purpose = kwargs['purpose']
        return self

    def stock_history_ohlc(self, day, **kwargs):
        self.calls.append((day, kwargs))
        if self.failure:
            raise self.failure
        return self.responses.get(day, rows(day, minutes=30 if day == DAY else None))

    def close(self):
        self.closed = True


def fields(embed):
    return {field.name: field.value for field in embed.fields}


def test_fixed_percentile_math_cached_request_and_unchanged_history():
    history = cache()
    client = Client({DAY: rows(DAY, minutes=30, anchor=200, spread=40)})
    result = range_session(DAY, NOW, history=history, client_factory=client.factory)
    assert isinstance(result, RangeResult)
    embed, returned = result.embed, result.history
    assert result.anchor == 200
    assert result.candle_close == session_time(DAY, 10)
    assert embed.timestamp == result.candle_close
    # percentile([1..20], .8) = 16.2; anchor200 gives +/-32.4.
    assert '$167.60–$232.40' in embed.description
    assert 'below estimated low and above estimated high' in fields(embed)['Range check']
    assert '$160.00–$240.00' in fields(embed)['Observed session']
    assert returned is history
    assert len(client.calls) == 1
    assert client.calls[0][1]['end_time'] == '10:00:00'
    assert client.purpose == 'unspecified' and client.closed
    assert 'PDT' in fields(embed)['Observed session']
    assert 'Not an 80% coverage guarantee' in fields(embed)['Method and sample']


def test_uncached_fetches_exact_twenty_preceding_sessions_then_current():
    client = Client()
    result = range_session(DAY, NOW, client_factory=client.factory)
    embed, history = result.embed, result.history
    assert [call[0] for call in client.calls] == [*_sessions(DAY), DAY]
    assert len(history) == SAMPLE_SIZE == 20
    assert '$99.00–$101.00' in embed.description
    assert client.closed


def test_incomplete_boundary_candle_cannot_affect_estimate_or_observed_range():
    current = rows(DAY, minutes=31)
    current[-1].update(high=10000, low=.01)
    client = Client({DAY: current})
    result = range_session(DAY, NOW, history=cache(), client_factory=client.factory)
    assert '$99.00–$101.00' in fields(result.embed)['Observed session']
    assert 'remain inside' in fields(result.embed)['Range check']


@pytest.mark.parametrize('damage', ['gap', 'duplicate', 'invalid', 'stale', 'future'])
def test_current_bad_or_stale_evidence_fails_closed(damage):
    current = rows(DAY, minutes=30)
    if damage == 'gap':
        current.pop(5)
    elif damage == 'duplicate':
        current.append(current[5].copy())
    elif damage == 'invalid':
        current[5]['low'] = -1
    elif damage == 'stale':
        current = current[:-2]
    else:
        current.append(dict(current[-1], timestamp=session_time(DAY, 10, 2)))
    client = Client({DAY: current})
    with pytest.raises(ValueError):
        range_session(DAY, NOW, history=cache(), client_factory=client.factory)
    assert client.closed


def test_staleness_boundary():
    client = Client({DAY: rows(DAY, minutes=29)})
    range_session(DAY, session_time(DAY, 10, 0) + timedelta(seconds=30),
                  history=cache(), client_factory=client.factory)
    with pytest.raises(ValueError, match='Stale'):
        range_session(DAY, session_time(DAY, 10, 0) + timedelta(seconds=31),
                      history=cache(), client_factory=client.factory)


@pytest.mark.parametrize('damage', ['missing_day', 'extra_day', 'missing_bar', 'rejected', 'future_receipt'])
def test_invalid_cache_rejected_before_client_creation(damage):
    history = cache()
    session = _sessions(DAY)[0]
    if damage == 'missing_day':
        history.pop(session)
    elif damage == 'extra_day':
        history[DAY] = new_record(DAY)
    elif damage == 'missing_bar':
        history[session]['bars'].pop()
    elif damage == 'rejected':
        history[session]['rejected_bars'] = 1
    else:
        history[session]['bars'][0]['available_at'] = (NOW + timedelta(seconds=1)).isoformat()
    client = Client()
    with pytest.raises(ValueError):
        range_session(DAY, NOW, history=history, client_factory=client.factory)
    assert client.purpose is None and not client.calls


def test_incomplete_fetched_history_aborts_and_closes_without_current_request():
    prior = _sessions(DAY)[0]
    client = Client({prior: rows(prior, minutes=389)})
    with pytest.raises(ValueError):
        range_session(DAY, NOW, client_factory=client.factory)
    assert [call[0] for call in client.calls] == [prior]
    assert client.closed


def test_provider_exception_closes_without_retry():
    client = Client(failure=RuntimeError('synthetic provider failure'))
    with pytest.raises(RuntimeError):
        range_session(DAY, NOW, client_factory=client.factory)
    assert len(client.calls) == 1 and client.closed


@pytest.mark.parametrize('day,now', [
    (DAY, session_time(DAY, 9, 30) + timedelta(seconds=59)),
    (DAY - timedelta(days=1), NOW),
    (date(2026, 9, 13), session_time(date(2026, 9, 13), 10)),
])
def test_invalid_target_never_creates_client(day, now):
    client = Client()
    with pytest.raises(ValueError):
        range_session(day, now, client_factory=client.factory)
    assert client.purpose is None


def test_early_close_complete_and_post_close_incomplete_rejected():
    day = date(2026, 11, 27)
    now = session_time(day, 14)
    history = cache(day, now)
    client = Client({day: rows(day)})
    result = range_session(day, now, history=history, client_factory=client.factory)
    assert client.calls[0][1]['end_time'] == '13:00:00'
    assert 'Session closed' in fields(result.embed)['Data as of']
    assert result.candle_close == session_time(day, 13)
    client = Client({day: rows(day, minutes=209)})
    with pytest.raises(ValueError, match='Incomplete'):
        range_session(day, now, history=history, client_factory=client.factory)
    assert client.closed


def test_dataclass_like_provider_rows_supported():
    client = Client({DAY: [SimpleNamespace(**row) for row in rows(DAY, minutes=30)]})
    result = range_session(DAY, NOW, history=cache(), client_factory=client.factory)
    assert '$99.00–$101.00' in fields(result.embed)['Observed session']


def test_retained_anchor_survives_revised_open_without_hiding_observed_prices():
    history = cache()
    initial = range_session(DAY, NOW, history=history, client_factory=Client().factory)
    revised = Client({DAY: rows(DAY, minutes=30, anchor=101)})
    result = range_session(DAY, NOW, history=history, anchor=initial.anchor, client_factory=revised.factory)
    assert result.anchor == initial.anchor == 100
    assert result.embed.description == initial.embed.description
    assert '$100.00–$102.00' in fields(result.embed)['Observed session']
    text = fields(result.embed)['Initial accepted open anchor']
    assert '$100.00' in text and 'process/session' in text and 'not a persisted forecast' in text
    assert 'restart' in text
    # Without retained caller state, a new calculation deliberately accepts the revised open.
    restarted = range_session(DAY, NOW, history=history, client_factory=revised.factory)
    assert restarted.anchor == 101 and restarted.embed.description != initial.embed.description


@pytest.mark.parametrize('anchor', [0, -1, float('inf'), float('-inf'), float('nan'), True, '100'])
def test_invalid_anchor_rejected_before_client_creation(anchor):
    client = Client()
    with pytest.raises(ValueError, match='anchor'):
        range_session(DAY, NOW, anchor=anchor, client_factory=client.factory)
    assert client.purpose is None


def test_each_response_records_actual_receipt_without_advancing_candle_cutoff():
    ticks = iter(NOW + timedelta(seconds=i * 10) for i in range(22))
    client = Client()
    result = range_session(DAY, NOW, client_factory=client.factory, clock=lambda: next(ticks))
    for index, day in enumerate(_sessions(DAY), 1):
        receipts = {bar['available_at'] for bar in result.history[day]['bars']}
        assert receipts == {(NOW + timedelta(seconds=index * 10)).isoformat()}
    # Slow retrieval does not expand today's requested window or the data-as-of time.
    assert client.calls[-1][1]['end_time'] == '10:00:00'
    assert result.candle_close == session_time(DAY, 10)
    assert result.embed.timestamp == result.candle_close
    assert '7:00:20' in fields(result.embed)['Data as of']
    assert client.closed


def test_current_response_uses_receipt_clock_and_fixed_market_cutoff(monkeypatch):
    import spy_setup.projected_range as module
    history = cache()
    calls = []
    original = module.update_record

    def capture(record, data, received_at, **kwargs):
        calls.append((received_at, kwargs['data_cutoff']))
        return original(record, data, received_at, **kwargs)

    monkeypatch.setattr(module, 'update_record', capture)
    ticks = iter([NOW, NOW + timedelta(seconds=25)])
    result = range_session(DAY, NOW, history=history, client_factory=Client().factory,
                           clock=lambda: next(ticks))
    assert calls == [(NOW + timedelta(seconds=25), session_time(DAY, 10))]
    assert result.candle_close == session_time(DAY, 10)


def test_receipts_after_cutoff_are_valid_when_actually_observed():
    received = NOW + timedelta(seconds=20)
    result = range_session(DAY, NOW, history=cache(now=received),
                           client_factory=Client().factory, clock=lambda: received)
    assert result.candle_close == session_time(DAY, 10)


def test_clock_before_cutoff_rejected_before_client_creation():
    client = Client()
    with pytest.raises(ValueError, match='Receipt clock'):
        range_session(DAY, NOW, client_factory=client.factory, clock=lambda: NOW - timedelta(seconds=1))
    assert client.purpose is None


def test_clock_going_backwards_after_response_closes_client():
    ticks = iter([NOW + timedelta(seconds=2), NOW + timedelta(seconds=1)])
    client = Client()
    with pytest.raises(ValueError, match='Receipt clock'):
        range_session(DAY, NOW, client_factory=client.factory, clock=lambda: next(ticks))
    assert len(client.calls) == 1 and client.closed
