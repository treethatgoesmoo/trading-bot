"""Offline diagnostic contracts; never contact provider or Discord."""
import asyncio
import logging

import pytest
import config
import spy_setup.cog as module
from test_spy_setup_cog import prepared, Channel


def test_delivery_correlates_and_does_not_repeat(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=module.__name__)
    cog, state, channel = prepared(tmp_path, monkeypatch)
    asyncio.run(cog._assess(state))
    asyncio.run(cog._assess(state))
    outcomes = [r.message for r in caplog.records if 'SPY setup delivery |' in r.message]
    assert len(outcomes) == 1
    assert 'outcome=sent' in outcomes[0] and 'durable=True' in outcomes[0]
    assert 'event=' in outcomes[0] and 'attempts=1' in outcomes[0]
    assert state['deliveries'][0]['id'] not in caplog.text
    assert len(channel.sent) == 1


def test_ambiguous_send_logs_safe_location_not_message(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=module.__name__)
    cog, state, _ = prepared(tmp_path, monkeypatch, Channel(RuntimeError('SECRET payload price=123.45')))
    asyncio.run(cog._assess(state))
    assert 'RuntimeError[' in caplog.text
    assert 'outcome=unknown reason=ambiguous_send' in caplog.text
    assert 'SECRET' not in caplog.text and '123.45' not in caplog.text


def test_disabled_publication_logs_suppression_once(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=module.__name__)
    cog, state, channel = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(config, 'PUBLICATION_ENABLED', False)
    asyncio.run(cog._assess(state))
    asyncio.run(cog._assess(state))
    assert caplog.text.count('outcome=suppressed reason=publication_disabled') == 1
    assert not channel.sent


def test_poll_summary_throttles_but_reports_health_change(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=module.__name__)
    cog, _, _ = prepared(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    cog._log_poll(99.0)
    cog._log_poll(99.0)
    clock[0] += 300
    cog._log_poll(clock[0])
    cog._persistence_fault = True
    cog._log_poll(clock[0])
    logs = [r for r in caplog.records if 'SPY poll outcome' in r.message]
    assert [r.levelno for r in logs] == [logging.INFO, logging.DEBUG, logging.INFO, logging.INFO]
    assert 'health=evidence_error' in logs[-1].message
    assert 'bars=' in logs[0].message and 'fresh=False' in logs[0].message
    assert 'elapsed=1.00s' in logs[0].message
    assert 'offline-test' not in caplog.text


def test_poll_exception_retains_behavior_and_safe_diagnostics(tmp_path, monkeypatch, caplog):
    cog, _, _ = prepared(tmp_path, monkeypatch)
    async def fail(now):
        raise ValueError('SECRET provider payload')
    monkeypatch.setattr(cog, '_poll_once_impl', fail)
    with pytest.raises(ValueError, match='SECRET'):
        asyncio.run(cog.poll_once())
    assert 'ValueError[' in caplog.text
    assert 'SECRET' not in caplog.text
