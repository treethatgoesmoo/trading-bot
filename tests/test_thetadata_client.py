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


def test_retired_api_absent():
    for name in ('index_history_ohlc', 'index_snapshots', 'option_trade_quotes',
                 'option_snapshots', 'list_expirations', 'subscription_summary'):
        assert not hasattr(ThetaDataClient, name)
