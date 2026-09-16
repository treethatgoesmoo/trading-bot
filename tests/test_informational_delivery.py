"""Offline delivery boundaries; fake channels only, no network/provider calls."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import config
from spy_setup import informational_delivery as delivery
from test_informational import DAY, at, record


class Channel:
    def __init__(self, *, error=None, blocked=False, inspect=None):
        self.sent = []
        self.error = error
        self.blocked = blocked
        self.inspect = inspect
        self.started = asyncio.Event()

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        if self.inspect:
            self.inspect()
        self.started.set()
        if self.blocked:
            await asyncio.Event().wait()
        if self.error:
            raise self.error


def prepared(tmp_path, monkeypatch, channel=None):
    channel = channel if channel is not None else Channel()
    monkeypatch.setattr(config, 'SPX_CHANNEL_ID', 123)
    state = {'record': record([100] * 15)}
    cog = SimpleNamespace(
        directory=tmp_path, _unloading=False, _persistence_fault=False,
        now=at(15), collecting=True, publishing=True,
        bot=SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 123 else None))
    cog._now = lambda: cog.now
    cog._collection_enabled = lambda: cog.collecting
    cog._publication_enabled = lambda: cog.publishing

    def guard(candidate):
        if candidate is not state or cog._persistence_fault:
            raise RuntimeError('durable reload required')
    cog._guard = guard
    return cog, state, channel


def journals(tmp_path):
    return sorted((tmp_path / 'informational-v1' / str(DAY)).glob('*.json'))


def journal(tmp_path):
    paths = journals(tmp_path)
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def test_durable_intent_precedes_send_and_duplicate_restart_does_not_retry(tmp_path, monkeypatch):
    async def run():
        def inspect():
            intent = journal(tmp_path)
            assert intent['status'] == 'unknown'
            assert intent['reason'] == 'send_reserved'
            assert intent['attempted_at'] == at(15).isoformat()
        channel = Channel(inspect=inspect)
        cog, state, _ = prepared(tmp_path, monkeypatch, channel)
        before = copy.deepcopy(state)
        assert await delivery.publish_events(cog, state) is None
        assert journal(tmp_path)['status'] == 'sent'
        await delivery.publish_events(cog, state)
        restarted, reloaded, _ = prepared(tmp_path, monkeypatch, channel)
        await delivery.publish_events(restarted, reloaded)
        assert len(channel.sent) == 1
        assert state == before
        assert delivery.delivery_counts(tmp_path, DAY) == dict(sent=1, failed=0, unknown=0, suppressed=0)
        assert {p.name for p in tmp_path.iterdir()} == {'informational-v1'}
    asyncio.run(run())


@pytest.mark.parametrize('flag', ['publishing', 'collecting'])
def test_disabled_observation_is_suppressed_and_never_replayed(tmp_path, monkeypatch, flag):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        setattr(cog, flag, False)
        await delivery.publish_events(cog, state)
        assert journal(tmp_path)['status'] == 'suppressed'
        assert journal(tmp_path)['attempted_at'] is None
        setattr(cog, flag, True)
        await delivery.publish_events(cog, state)
        assert not channel.sent
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['_unloading', '_persistence_fault'])
def test_unavailable_collector_does_not_prepare_or_send(tmp_path, monkeypatch, fault):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        setattr(cog, fault, True)
        await delivery.publish_events(cog, state)
        assert not channel.sent and not journals(tmp_path)
    asyncio.run(run())


def test_wrong_state_is_rejected_by_guard(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        with pytest.raises(RuntimeError, match='durable reload'):
            await delivery.publish_events(cog, copy.deepcopy(state))
        assert not channel.sent
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['unknown', 'rejected', 'missing'])
def test_ambiguous_rejected_and_missing_channel_never_retry(tmp_path, monkeypatch, kind, caplog):
    async def run():
        error = (discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'private-secret')
                 if kind == 'rejected' else RuntimeError('private-secret'))
        channel = Channel(error=error)
        cog, state, _ = prepared(tmp_path, monkeypatch, channel)
        if kind == 'missing':
            cog.bot.get_channel = lambda _: None
        await delivery.publish_events(cog, state)
        assert journal(tmp_path)['status'] == ('unknown' if kind == 'unknown' else 'failed')
        channel.error = None
        cog.bot.get_channel = lambda _: channel
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == (0 if kind == 'missing' else 1)
        assert 'private-secret' not in caplog.text
    asyncio.run(run())


def test_cancellation_preserves_unknown_intent_and_propagates(tmp_path, monkeypatch):
    async def run():
        channel = Channel(blocked=True)
        cog, state, _ = prepared(tmp_path, monkeypatch, channel)
        task = asyncio.create_task(delivery.publish_events(cog, state))
        await channel.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal(tmp_path)['status'] == 'unknown'
        assert journal(tmp_path)['reason'] == 'cancelled_send'
        channel.blocked = False
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
    asyncio.run(run())


def test_timeout_is_unknown_never_retried(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(delivery, 'SEND_TIMEOUT_SECONDS', .01)
        channel = Channel(blocked=True)
        cog, state, _ = prepared(tmp_path, monkeypatch, channel)
        await delivery.publish_events(cog, state)
        assert journal(tmp_path)['status'] == 'unknown'
        assert journal(tmp_path)['reason'] == 'send_timeout'
        channel.blocked = False
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['write', 'link', 'directory_sync'])
def test_reservation_disk_failure_prevents_send_and_does_not_escape(tmp_path, monkeypatch, phase, caplog):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        def fail(*_args, **_kwargs):
            raise OSError('private-provider-payload')
        if phase == 'write':
            monkeypatch.setattr(delivery, '_write_temporary', fail)
        elif phase == 'link':
            monkeypatch.setattr(delivery.os, 'link', fail)
        else:
            # Fail after the exclusive journal exists, just before authorization.
            original = delivery._sync_directory
            def sync(path):
                if journals(tmp_path):
                    fail()
                original(path)
            monkeypatch.setattr(delivery, '_sync_directory', sync)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert 'private-provider-payload' not in caplog.text
        assert not list(tmp_path.rglob('*.tmp'))
        if phase == 'directory_sync':
            assert journal(tmp_path)['status'] == 'unknown'
    asyncio.run(run())


def test_file_fsync_failure_never_authorizes_send(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        def fail(_fd):
            raise OSError('fsync failed')
        monkeypatch.setattr(delivery.os, 'fsync', fail)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert not journals(tmp_path)
        assert not list(tmp_path.rglob('*.tmp'))
    asyncio.run(run())


def test_atomic_ack_replace_failure_preserves_original_intent(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        def fail(*_args):
            raise OSError('replace failed')
        monkeypatch.setattr(delivery.os, 'replace', fail)
        await delivery.publish_events(cog, state)
        assert journal(tmp_path)['status'] == 'unknown'
        assert journal(tmp_path)['reason'] == 'send_reserved'
        assert not list(tmp_path.rglob('*.tmp'))
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
    asyncio.run(run())


def test_acknowledgement_write_failure_keeps_unknown_and_blocks_retry(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        def fail(*_args):
            raise OSError('disk full')
        monkeypatch.setattr(delivery, '_finish', fail)
        await delivery.publish_events(cog, state)
        assert journal(tmp_path)['status'] == 'unknown'
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
    asyncio.run(run())


@pytest.mark.parametrize('change,reason', [
    ('stale', 'expired_before_send'), ('closed', 'expired_before_send'),
    ('disabled', 'publication_disabled'), ('collection', 'collection_disabled'),
    ('shutdown', 'shutdown'),
])
def test_recheck_after_reservation_blocks_late_or_disabled_send(tmp_path, monkeypatch, change, reason):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        reserve = delivery._reserve
        def altered(*args):
            result = reserve(*args)
            if change == 'stale':
                cog.now = at(15, 91)
            elif change == 'closed':
                cog.now = at(390)
            elif change == 'disabled':
                cog.publishing = False
            elif change == 'collection':
                cog.collecting = False
            else:
                cog._unloading = True
            return result
        monkeypatch.setattr(delivery, '_reserve', altered)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert journal(tmp_path)['status'] == 'suppressed'
        assert journal(tmp_path)['reason'] == reason
    asyncio.run(run())


def test_persistence_fault_after_reservation_fails_closed(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        reserve = delivery._reserve
        def altered(*args):
            result = reserve(*args)
            cog._persistence_fault = True
            return result
        monkeypatch.setattr(delivery, '_reserve', altered)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert journal(tmp_path)['status'] == 'unknown'
    asyncio.run(run())


def test_market_closed_builds_no_intents(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        cog.now = at(390)
        await delivery.publish_events(cog, state)
        assert not channel.sent and not journals(tmp_path)
    asyncio.run(run())


def test_later_events_rechecked_after_first_send_wait(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        state['record'] = record([100] * 20 + [110])
        cog.now = at(21)
        channel.inspect = lambda: setattr(cog, 'now', at(21, 91))
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
        assert delivery.delivery_counts(tmp_path, DAY) == dict(sent=1, failed=0, unknown=0, suppressed=1)
    asyncio.run(run())


@pytest.mark.parametrize('contents', ['{', '[]', '{"status":"sent"}'])
def test_corrupt_existing_intent_is_not_replaced_or_retried(tmp_path, monkeypatch, contents):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        event = delivery.build_events(state['record'], cog._now())[0]
        reservation = delivery._reserve(tmp_path, event, cog._now(), 'unknown', 'send_reserved')
        path = reservation[0]
        path.write_text(contents)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert path.read_text() == contents
        assert delivery.delivery_counts(tmp_path, DAY)['unknown'] == 1
    asyncio.run(run())


def test_existing_directory_at_journal_path_also_blocks_send(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        event = delivery.build_events(state['record'], cog._now())[0]
        folder = delivery._prepare_folder(tmp_path, DAY)
        path = folder / delivery._filename(event['id'])
        path.mkdir()
        await delivery.publish_events(cog, state)
        assert not channel.sent and path.is_dir()
        assert delivery.delivery_counts(tmp_path, DAY)['unknown'] == 1
    asyncio.run(run())


def test_exclusive_link_race_never_overwrites_existing_intent(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        original = delivery.os.link
        def race(source, target):
            Path(target).write_text('existing owner')
            original(source, target)
        monkeypatch.setattr(delivery.os, 'link', race)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert journals(tmp_path)[0].read_text() == 'existing owner'
        assert not list(tmp_path.rglob('*.tmp'))
    asyncio.run(run())


def test_delivery_counts_missing_is_read_only_and_corrupt_is_unknown(tmp_path):
    missing = tmp_path / 'absent'
    assert delivery.delivery_counts(missing, DAY) == dict(sent=0, failed=0, unknown=0, suppressed=0)
    assert not missing.exists()
    event = delivery.build_events(record([100] * 15), at(15))[0]
    for index, status in enumerate(delivery.STATUSES):
        item = dict(event, id=event['id'] + f'-{index}')
        delivery._reserve(tmp_path, item, at(15), status, 'test')
    folder = tmp_path / 'informational-v1' / str(DAY)
    (folder / 'corrupt.json').write_text('{')
    (folder / 'ignored.tmp').write_text('{')
    before = {p: p.read_bytes() for p in folder.iterdir()}
    assert delivery.delivery_counts(tmp_path, DAY) == dict(sent=1, failed=1, unknown=2, suppressed=1)
    assert {p: p.read_bytes() for p in folder.iterdir()} == before


def test_delivery_counts_scan_error_does_not_claim_zero(tmp_path, monkeypatch):
    def denied(_path):
        raise PermissionError('cannot inspect evidence')
    monkeypatch.setattr(Path, 'iterdir', denied)
    with pytest.raises(PermissionError):
        delivery.delivery_counts(tmp_path, DAY)


@pytest.mark.parametrize('closes,minutes,timeframe', [([100] * 15, 15, None),
    ([100] * 20 + [110], 21, '1'), ([100] * 104 + [90], 105, '5')])
def test_embeds_are_informational_spy_and_pacific(closes, minutes, timeframe):
    events = delivery.build_events(record(closes), at(minutes))
    event = (events[0] if timeframe is None else
             next(e for e in events if e['kind'].startswith('bb_') and e['timeframe'] == timeframe))
    embed = delivery.informational_embed(event).to_dict()
    assert 'SPY' in embed['title']
    assert 'INFORMATIONAL — NOT AN ENTRY SIGNAL' in embed['description']
    assert 'PDT' in embed['description']
    assert 'not SPX option prices' in embed['description']
    if timeframe:
        assert timeframe + 'm' in embed['title']
        assert '20 periods / 2σ' in embed['fields'][0]['name']
        assert 'population standard deviation' in embed['fields'][0]['value']
