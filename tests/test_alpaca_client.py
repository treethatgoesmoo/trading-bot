from datetime import date, datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

import config
from core import market_data_usage as usage
from core.alpaca_client import AlpacaClient, MarketDataConfigurationError, MarketDataError
from core.feed_check import coverage

DAY = date(2026, 9, 8)


def row(**changes):
    return dict(t="2026-09-08T13:30:00Z", o=100, h=102, l=99, c=101, v=1200) | changes


def page(*rows, next_token=None):
    return {"bars": {"SPY": list(rows)}, "next_page_token": next_token}


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_KEY_ID", "private-key")
    monkeypatch.setattr(config, "ALPACA_API_SECRET_KEY", "private-secret")
    monkeypatch.setattr(config, "MARKET_DATA_AUDIT_START_DATE", "")


def client_with(*pages):
    requests = []
    pages = iter(pages)

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return BytesIO(json.dumps(next(pages)).encode())

    return AlpacaClient(opener=opener), requests


def test_explicit_feed_utc_cutoff_and_ordered_pages():
    client, requests = client_with(
        page(row(t="2026-09-08T13:31:00Z", v=0), next_token="opaque+/token"),
        page(row()),
    )
    bars = client.stock_history_ohlc(DAY, end_time="09:32:00")
    assert [bar.volume for bar in bars] == [1200, 0]
    assert bars[0].timestamp == datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
    first, timeout = requests[0]
    assert timeout == 8
    url = urlsplit(first.full_url)
    assert (url.scheme, url.netloc, url.path) == ("https", "data.alpaca.markets", "/v2/stocks/bars")
    params = parse_qs(url.query)
    assert params["feed"] == ["iex"]
    assert params["symbols"] == ["SPY"]
    assert params["timeframe"] == ["1Min"]
    assert params["start"] == ["2026-09-08T13:30:00+00:00"]
    assert params["end"] == ["2026-09-08T13:31:59+00:00"]
    assert first.get_header("Apca-api-key-id") == "private-key"
    assert first.get_header("Apca-api-secret-key") == "private-secret"
    assert "private" not in first.full_url
    assert parse_qs(urlsplit(requests[1][0].full_url).query)["page_token"] == ["opaque+/token"]


def test_winter_early_close_boundaries_and_no_synthetic_minutes():
    client, requests = client_with({"bars": {}, "next_page_token": None})
    assert client.stock_history_ohlc(date(2026, 11, 27), end_time="13:00:00") == []
    query = parse_qs(urlsplit(requests[0][0].full_url).query)
    assert query["start"] == ["2026-11-27T14:30:00+00:00"]
    assert query["end"] == ["2026-11-27T17:59:59+00:00"]


@pytest.mark.parametrize("change", [
    {"v": -1}, {"v": True}, {"v": 1.5}, {"v": None},
    {"o": 0}, {"o": "100"}, {"h": 99}, {"l": 101}, {"c": float("nan")}, {"v": 10**400},
    {"t": "2026-09-08T13:30:00"}, {"t": "2026-09-08T13:30:00.000000001Z"},
    {"t": "2026-09-08T13:31:00Z"}, {"t": "2026-09-09T13:30:00Z"},
])
def test_malformed_or_partial_candle_rejects_whole_request(change):
    client, _ = client_with(page(row(**change)))
    with pytest.raises(MarketDataError):
        client.stock_history_ohlc(DAY, end_time="09:31:00")


@pytest.mark.parametrize("pages", [
    [page(row(), next_token="next"), page(row())],
    [page(next_token="again"), page(next_token="again")],
    [page(next_token="1"), page(next_token="2"), page(next_token="3")],
    [{"bars": {"QQQ": []}}],
])
def test_duplicate_or_unexpected_pages_are_rejected(pages):
    client, _ = client_with(*pages)
    with pytest.raises(MarketDataError):
        client.stock_history_ohlc(DAY)


def test_missing_credential_prevents_request(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_API_SECRET_KEY", "")
    client, requests = client_with()
    with pytest.raises(MarketDataConfigurationError):
        client.stock_history_ohlc(DAY)
    assert requests == []


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
def test_http_failure_exposes_only_status(status):
    body = BytesIO(b"private")

    def opener(request, *, timeout):
        raise HTTPError(request.full_url, status, "PRIVATE MESSAGE", {}, body)

    with pytest.raises(MarketDataError, match=f"^Alpaca bars returned HTTP {status}$"):
        AlpacaClient(opener=opener).stock_history_ohlc(DAY)
    assert body.closed


def test_audit_keeps_request_identity_without_keys_or_market_data(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MARKET_DATA_AUDIT_START_DATE", DAY.isoformat())
    monkeypatch.setattr(config, "MARKET_DATA_AUDIT_DAYS", 1)
    monkeypatch.setattr(usage, "MARKET_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_now", lambda: datetime(2026, 9, 8, 14, tzinfo=timezone.utc))
    client, _ = client_with(page(row()))
    client.stock_history_ohlc(DAY)
    audit = next((tmp_path / "audit").glob("*.jsonl")).read_text()
    start, finish = [json.loads(line) for line in audit.splitlines()]
    assert client._last_request_id == start["request_id"] == finish["request_id"]
    assert finish["success"] and finish["rows"] == 1
    assert "private" not in audit and "1200" not in audit


def test_feed_check_reports_missing_completed_minutes():
    client, _ = client_with(page(row(), row(t="2026-09-08T13:32:00Z")))
    now = datetime(2026, 9, 8, 13, 34, 20, tzinfo=timezone.utc)
    assert coverage(client, DAY, now) == (3, ["09:31"])
