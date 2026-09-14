"""Offline tests: never construct an authenticated SDK client."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from core import theta_usage
from core.thetadata_client import ThetaDataClient, ThetaDataError, ThetaDataConfigurationError

DAY = date(2026, 9, 8)


def row(**changes):
    result = dict(timestamp='2026-09-08T09:30:00-04:00', open=100, high=102,
                  low=99, close=101, volume=1200)
    result.update(changes)
    return result


def adapter(rows):
    sdk = SimpleNamespace(stock_history_ohlc=Mock(return_value=rows))
    return ThetaDataClient(client=sdk, purpose='spy_setup'), sdk


def test_endpoint_and_volume_contract():
    client, sdk = adapter([row(timestamp='2026-09-08T09:31:00-04:00', volume=0), row()])
    bars = client.stock_history_ohlc(DAY)
    sdk.stock_history_ohlc.assert_called_once_with(
        symbol='SPY', interval='1m', start_date=DAY, end_date=DAY,
        start_time='09:30:00', end_time='16:00:00', venue='nqb')
    assert bars[0].timestamp == datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    assert [bar.volume for bar in bars] == [1200, 0]


@pytest.mark.parametrize('changes', [
    {'volume': None}, {'volume': -1}, {'volume': float('nan')},
    {'volume': float('inf')}, {'volume': True}, {'volume': 1.5},
    {'open': 0}, {'high': 100}, {'low': 102}, {'close': float('inf')},
    {'timestamp': '2026-09-08T09:30:00'}, {'timestamp': None},
    {'timestamp': 'NaT'}, {'timestamp': 1234},
    {'timestamp': '2026-09-08T09:30:01-04:00'},
    {'timestamp': '2026-09-08T09:30:00.000000001-04:00'},
    {'timestamp': '2026-09-09T09:30:00-04:00'},
])
def test_bad_row_fails_entire_response(changes):
    client, _ = adapter([row(), row(**changes)])
    with pytest.raises(ThetaDataError):
        client.stock_history_ohlc(DAY)


@pytest.mark.parametrize('changes,expected_low,expected_high', [
    ({'open': 763.4665, 'high': 763.47, 'low': 763.2800000000001, 'close': 763.28}, 763.28, 763.47),
    ({'open': 100, 'high': 100.99999999999999, 'low': 99, 'close': 101}, 99, 101),
    ({'open': 100, 'high': 100 - 5e-10, 'low': 100 + 5e-10, 'close': 100}, 100, 100),
    ({'open': 100, 'high': 102, 'low': 99, 'close': 101}, 99, 102),
])
def test_tiny_extremum_rounding_is_normalized_outward(changes, expected_low, expected_high):
    source = pd.DataFrame([row(**changes)])
    original = source.copy(deep=True)
    bar = adapter(source)[0].stock_history_ohlc(DAY)[0]
    assert bar.low == expected_low
    assert bar.high == expected_high
    assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
    assert bar.open == changes['open'] and bar.close == changes['close']
    assert bar.volume == 1200
    pd.testing.assert_frame_equal(source, original)


@pytest.mark.parametrize('changes', [
    {'low': 100 + 2e-9}, {'high': 101 - 2e-9},
    {'low': 100.01}, {'high': 100.99},
    # A relative tolerance would incorrectly allow this at large prices.
    {'open': 1e9, 'close': 1e9, 'high': 1e9, 'low': 1e9 + 0.01},
    {'open': 1e9, 'close': 1e9, 'high': 1e9 - 0.01, 'low': 1e9 - 1},
    {'open': 1e-10, 'close': 1e-10, 'high': 0, 'low': 1e-10},
    {'open': 1e-10, 'close': 1e-10, 'high': -1e-10, 'low': 1e-10},
    {'open': 0}, {'low': -1e-10}, {'high': None}, {'low': float('nan')},
])
def test_rounding_tolerance_does_not_repair_invalid_ohlc(changes):
    with pytest.raises(ThetaDataError):
        adapter([row(**changes)])[0].stock_history_ohlc(DAY)


def test_missing_volume_and_duplicate_timestamps_fail():
    missing = row()
    del missing['volume']
    for rows in ([missing], [row(), row()]):
        with pytest.raises(ThetaDataError):
            adapter(rows)[0].stock_history_ohlc(DAY)


@pytest.mark.parametrize('rows', [None, [], pd.DataFrame()])
def test_empty_response(rows):
    assert adapter(rows)[0].stock_history_ohlc(DAY) == []


def test_dataframe_normalization_does_not_mutate():
    frame = pd.DataFrame([row()])
    frame.columns = [f' {name.upper()} ' for name in frame.columns]
    original = frame.copy(deep=True)
    assert adapter(frame)[0].stock_history_ohlc(DAY)[0].volume == 1200
    pd.testing.assert_frame_equal(frame, original)


def test_duplicate_normalized_columns_fail():
    frame = pd.DataFrame([row()])
    frame[' Volume '] = 23
    with pytest.raises(ThetaDataError):
        adapter(frame)[0].stock_history_ohlc(DAY)


def test_provider_exception_is_chained_but_not_interpolated():
    client, sdk = adapter([])
    error = RuntimeError('potentially sensitive provider response')
    sdk.stock_history_ohlc.side_effect = error
    with pytest.raises(ThetaDataError) as caught:
        client.stock_history_ohlc(DAY)
    assert caught.value.__cause__ is error
    assert 'sensitive' not in str(caught.value)


def test_configuration_error_remains_distinct(monkeypatch):
    client = ThetaDataClient(api_key='')
    client._api_key = ''
    with pytest.raises(ThetaDataConfigurationError):
        client.stock_history_ohlc(DAY)


def test_request_telemetry_and_lineage(monkeypatch):
    ticket = SimpleNamespace(request_id='request-123')
    begin = Mock(return_value=ticket)
    finish = Mock()
    monkeypatch.setattr(theta_usage, 'begin_request', begin)
    monkeypatch.setattr(theta_usage, 'finish_request', finish)
    client, _ = adapter([row()])
    client.stock_history_ohlc(DAY)
    begin.assert_called_once_with('stock_history_ohlc', 'spy_setup')
    finish.assert_called_once_with(ticket, rows=1)
    assert client._last_request_id == 'request-123'


def test_request_error_telemetry(monkeypatch):
    ticket = SimpleNamespace(request_id='failed-request')
    monkeypatch.setattr(theta_usage, 'begin_request', Mock(return_value=ticket))
    finish = Mock()
    monkeypatch.setattr(theta_usage, 'finish_request', finish)
    client, sdk = adapter([])
    error = RuntimeError('network')
    sdk.stock_history_ohlc.side_effect = error
    with pytest.raises(ThetaDataError):
        client.stock_history_ohlc(DAY)
    finish.assert_called_once_with(ticket, error=error)


def test_inactive_telemetry_clears_previous_request_id(monkeypatch):
    monkeypatch.setattr(theta_usage, 'begin_request', Mock(return_value=None))
    client, _ = adapter([])
    client._last_request_id = 'old'
    client.stock_history_ohlc(DAY)
    assert client._last_request_id is None


def test_close_is_lazy_and_idempotent(monkeypatch):
    client, sdk = adapter([])
    sdk.close = Mock()
    client.close()
    client.close()
    sdk.close.assert_called_once_with()
    assert client._client is None
    client = ThetaDataClient()
    monkeypatch.setattr(client, '_get_client', Mock(side_effect=AssertionError('must remain lazy')))
    client.close()


def test_sdk_without_public_close_supported():
    client, _ = adapter([])
    client.close()
    assert client._client is None


@pytest.mark.parametrize('kwargs', [{'symbol': 'SPX'}, {'interval': '5m'}])
def test_unsupported_request_rejected_before_network(kwargs):
    client, sdk = adapter([])
    with pytest.raises(ValueError):
        client.stock_history_ohlc(DAY, **kwargs)
    sdk.stock_history_ohlc.assert_not_called()


def placeholder(**changes):
    result = row(timestamp='2026-09-08T09:35:00-04:00', open=float('nan'),
                 high=float('nan'), low=float('nan'), close=float('nan'), volume=0, count=0)
    result.update(changes)
    return result


@pytest.mark.parametrize('zero', [0, 0.0])
def test_empty_exact_end_boundary_skipped_without_mutating_response(zero):
    frame = pd.DataFrame([row(), placeholder(volume=zero, count=zero)])
    original = frame.copy(deep=True)
    bars = adapter(frame)[0].stock_history_ohlc(DAY, end_time='09:35:00')
    assert len(bars) == 1
    assert bars[0].volume == 1200
    pd.testing.assert_frame_equal(frame, original)


def test_placeholder_only_response_is_empty():
    assert adapter([placeholder()])[0].stock_history_ohlc(DAY, end_time='09:35:00') == []


@pytest.mark.parametrize('day,stamp', [
    (DAY, '2026-09-08T13:35:00+00:00'),
    (date(2026, 1, 6), '2026-01-06T14:35:00+00:00'),
])
def test_boundary_uses_eastern_session_time_across_dst(day, stamp):
    client, _ = adapter([placeholder(timestamp=stamp)])
    assert client.stock_history_ohlc(day, end_time='09:35:00') == []


def test_valid_end_boundary_bar_is_not_dropped():
    bars = adapter([row(timestamp='2026-09-08T09:35:00-04:00')])[0].stock_history_ohlc(
        DAY, end_time='09:35:00')
    assert len(bars) == 1


@pytest.mark.parametrize('changes', [
    {'timestamp': '2026-09-08T09:34:00-04:00'},  # Interior gap, not boundary.
    {'timestamp': '2026-09-08T09:36:00-04:00'},
    {'timestamp': '2026-09-09T09:35:00-04:00'},
    {'timestamp': '2026-09-08T09:35:01-04:00'},
    {'timestamp': '2026-09-08T09:35:00'},
    {'open': 100}, {'high': 102}, {'low': 99}, {'close': 101},
    {'volume': 1}, {'count': 1}, {'volume': False}, {'count': False},
    {'volume': '0'}, {'count': '0'}, {'count': None},
    {'count': float('nan')}, {'volume': float('nan')},
])
def test_placeholder_exception_never_hides_malformed_data(changes):
    client, _ = adapter([row(), placeholder(**changes)])
    with pytest.raises(ThetaDataError):
        client.stock_history_ohlc(DAY, end_time='09:35:00')


def test_placeholder_requires_count_and_unique_timestamp():
    missing_count = placeholder()
    del missing_count['count']
    for rows in ([missing_count], [placeholder(), placeholder()],
                 [row(timestamp='2026-09-08T09:35:00-04:00'), placeholder()]):
        with pytest.raises(ThetaDataError):
            adapter(rows)[0].stock_history_ohlc(DAY, end_time='09:35:00')


@pytest.mark.parametrize('kwargs', [
    {'end_time': 'bad'}, {'end_time': None}, {'end_time': '25:00:00'},
    {'end_time': '9:35:00'}, {'end_time': '09:35'},
    {'end_time': '09:35:00-04:00'}, {'start_time': 'bad'},
    {'start_time': '16:00:00'}, {'start_time': '17:00:00'},
])
def test_invalid_request_times_fail_before_network(kwargs):
    client, sdk = adapter([])
    with pytest.raises(ValueError):
        client.stock_history_ohlc(DAY, **kwargs)
    sdk.stock_history_ohlc.assert_not_called()


def test_retired_api_absent():
    for name in ('index_history_ohlc', 'index_snapshots', 'option_trade_quotes',
                 'option_snapshots', 'list_expirations', 'subscription_summary'):
        assert not hasattr(ThetaDataClient, name)
