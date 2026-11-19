from datetime import datetime
import json
from pathlib import Path

import pytest

import config
from core import market_data_usage as usage


@pytest.fixture(autouse=True)
def audit(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "MARKET_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "MARKET_DATA_AUDIT_START_DATE", "2026-09-04")
    monkeypatch.setattr(config, "MARKET_DATA_AUDIT_DAYS", 30)
    return tmp_path / "audit"


@pytest.mark.parametrize("stamp,expected", [
    ("2026-09-04T03:59:59+00:00", False),
    ("2026-09-04T04:00:00+00:00", True),
    ("2026-10-04T03:59:59+00:00", True),
    ("2026-10-04T04:00:00+00:00", False),
])
def test_audit_window_uses_eastern_dates(stamp, expected):
    assert usage.audit_active(datetime.fromisoformat(stamp)) is expected


def test_completion_stays_in_original_file_and_redacts_failures(audit, monkeypatch):
    clock = datetime.fromisoformat("2026-10-04T03:59:59+00:00")
    monkeypatch.setattr(usage, "_now", lambda: clock)
    assert usage.begin_request("unrecognized-endpoint") is None
    ticket = usage.begin_request("stock_history_ohlc", "private purpose")
    path = audit / "usage-2026-10-03.jsonl"
    assert json.loads(path.read_text())["event"] == "start"
    clock = datetime.fromisoformat("2026-10-04T04:01:00+00:00")
    usage.finish_request(ticket, error=RuntimeError("private failure"))
    start, finish = [json.loads(line) for line in path.read_text().splitlines()]
    assert start["purpose"] == "unspecified"
    assert start["request_id"] == finish["request_id"]
    assert finish["error_class"] == "RuntimeError" and not finish["success"]
    assert "private" not in path.read_text()
    assert list(audit.glob("*.jsonl")) == [path]


def test_metadata_write_failure_is_nonfatal(monkeypatch, caplog):
    monkeypatch.setattr(usage, "_now", lambda: datetime.fromisoformat("2026-09-04T14:00:00+00:00"))

    def fail(*args, **kwargs):
        raise OSError("private path")

    monkeypatch.setattr(Path, "open", fail)
    ticket = usage.begin_request("stock_history_ohlc")
    usage.finish_request(ticket, rows=0)
    assert "could not be persisted" in caplog.text and "private path" not in caplog.text
