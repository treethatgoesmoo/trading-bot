"""Offline integration of real collection, informational delivery and reporting.

No cog_load, gateway, SDK or background monitor is started. poll_once and its
provider worker run normally, using a synthetic provider and in-memory channel.
"""
from datetime import date, timedelta
import json
from types import SimpleNamespace

import pytest

import config
from core.session_bars import session_time
from spy_setup.cog import SpySetupCog
from spy_setup.informational_delivery import delivery_counts

DAY = date(2026, 9, 15)


class Provider:
    def __init__(self, trace):
        self.trace = trace
        self.calls = []

    def stock_history_ohlc(self, day, **kwargs):
        self.calls.append((day, kwargs))
        self.trace.append('current' if day == DAY else 'prior')
        if day != DAY:
            return []  # No history available, deliberately.
        return [dict(timestamp=(session_time(DAY, 9, 30) + timedelta(minutes=i)).isoformat(),
                     open=100., high=101., low=99., close=100., volume=100.)
                for i in range(15)]


class Channel:
    def __init__(self, trace):
        self.trace = trace
        self.sent = []
        self.before_send = None

    async def send(self, **kwargs):
        if self.before_send:
            self.before_send()
        self.trace.append('send')
        self.sent.append(kwargs)


@pytest.fixture
def service(tmp_path, monkeypatch):
    for name, value in [('COLLECTION_ENABLED', True), ('PUBLICATION_ENABLED', True),
                        ('THETADATA_API_KEY', 'offline-test'), ('SPX_CHANNEL_ID', 123)]:
        monkeypatch.setattr(config, name, value)
    trace = []
    provider, channel = Provider(trace), Channel(trace)
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 123 else None)
    cog = SpySetupCog(bot, provider, directory=tmp_path)
    clock = [session_time(DAY, 9, 45) + timedelta(seconds=5)]
    cog._now = lambda: clock[0]
    return SimpleNamespace(cog=cog, provider=provider, channel=channel, trace=trace,
                           clock=clock, directory=tmp_path, bot=bot)


def files(directory):
    """Capture bytes and paths to detect reporting mutations or new journals."""
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


@pytest.mark.asyncio
async def test_real_poll_range_without_setup_or_history_saved_before_send(service):
    s = service

    def persisted_before_send():
        state = s.cog.store.load(DAY)
        assert len(state['record']['bars']) == 15
        assert state['requests']['current']['succeeded'] == 1
        assert state['observations'] == state['deliveries'] == []
        assert state['previous_bars'] == []
        journals = list(s.directory.glob('informational-v2/*/*.json'))
        assert len(journals) == 1
        assert json.loads(journals[0].read_text())['status'] == 'unknown'
        assert s.trace == ['current']  # No history request precedes the send.

    s.channel.before_send = persisted_before_send
    await s.cog.poll_once()
    assert s.trace == ['current', 'send'] + ['prior'] * 5
    assert len(s.channel.sent) == 1
    embed = s.channel.sent[0]['embed']
    assert embed.title == '📏 SPY OPENING RANGE'
    assert not embed.fields and not embed.footer.text
    assert '\n' not in embed.description
    state = s.cog.store.load(DAY)
    assert state['observations'] == state['deliveries'] == []
    assert state['previous_bars'] == [] and not state['prior_complete']
    assert delivery_counts(s.directory, DAY) == dict(sent=1, failed=0, unknown=0, suppressed=0)

    # Repeated real polls and a new cog over the same evidence must not resend.
    s.channel.before_send = None
    await s.cog.poll_once()
    restart = SpySetupCog(s.bot, s.provider, directory=s.directory)
    restart._now = s.cog._now
    await restart.poll_once()
    assert len(s.channel.sent) == 1


@pytest.mark.asyncio
async def test_real_poll_publication_suppression_survives_enable_and_restart(service, monkeypatch):
    s = service
    monkeypatch.setattr(config, 'PUBLICATION_ENABLED', False)
    await s.cog.poll_once()
    assert not s.channel.sent
    assert delivery_counts(s.directory, DAY)['suppressed'] == 1
    monkeypatch.setattr(config, 'PUBLICATION_ENABLED', True)
    await s.cog.poll_once()
    restart = SpySetupCog(s.bot, s.provider, directory=s.directory)
    restart._now = s.cog._now
    await restart.poll_once()
    assert not s.channel.sent
    assert delivery_counts(s.directory, DAY) == dict(sent=0, failed=0, unknown=0, suppressed=1)


@pytest.mark.asyncio
async def test_day_status_and_automatic_report_keep_separate_counts(service):
    s = service
    await s.cog.poll_once()
    s.clock[0] = session_time(DAY, 16, 6)
    before = files(s.directory)
    requests = list(s.provider.calls)
    replies = []

    async def reply(**kwargs):
        replies.append(kwargs)

    ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=reply)
    await s.cog.day.callback(s.cog, ctx, DAY.isoformat())
    await s.cog.cmd_status.callback(s.cog, ctx)
    assert files(s.directory) == before
    assert s.provider.calls == requests
    day_embed = replies[0]['embed']
    fields = {field.name: field.value for field in day_embed.fields}
    assert 'Formed: 0 | Triggered entries: 0' in fields['Observed setups']
    assert 'sent: 0' in fields['Recorded alert delivery']
    assert 'sent: 1' in fields['Informational range / BB notifications']
    status = replies[1]['content']
    assert "Deliveries: {'sent': 0" in status
    assert 'Informational range / BB (today): sent: 1' in status

    await s.cog.daily_once()
    assert len(s.channel.sent) == 2  # One range announcement, one daily report.
    report = s.channel.sent[-1]['embed']
    assert report.to_dict() == day_embed.to_dict()
    after = files(s.directory)
    assert all(after[name] == value for name, value in before.items())
    assert set(after) - set(before) == {f'daily-delivery-{DAY}.json'}
    assert json.loads(after[f'daily-delivery-{DAY}.json'])['status'] == 'sent'
    assert s.provider.calls == requests
    await s.cog.daily_once()
    assert len(s.channel.sent) == 2


@pytest.mark.asyncio
async def test_real_poll_setup_and_informational_messages_have_independent_ledgers(tmp_path, monkeypatch):
    # Reuse the established setup fixture to expose the changed total-message
    # contract without rewriting the existing collector test in this task.
    from test_spy_setup_cog import prepared
    from test_spy_setup_signals import breakout

    cog, state, channel = prepared(tmp_path, monkeypatch)

    class BreakoutProvider:
        def stock_history_ohlc(self, day, **kwargs):
            return [breakout()]

    cog.theta_client = BreakoutProvider()
    await cog.poll_once()
    assert len(channel.sent) == 4
    setup_alerts = [item['embed'] for item in channel.sent if 'SETUP ·' in item['embed'].title]
    assert len(setup_alerts) == 1
    assert 'FORMING' in setup_alerts[0].title
    assert 'Not triggered' in setup_alerts[0].description
    assert all(not item['embed'].fields and not item['embed'].footer.text
               and '\n' not in item['embed'].description for item in channel.sent)
    from spy_setup.early_shift_delivery import delivery_counts as early_counts
    assert early_counts(tmp_path, state['session_date'])['sent'] == 1
    informational = [item['embed'] for item in channel.sent
                     if 'OPENING RANGE' in item['embed'].title]
    assert len(informational) == 2
    assert {embed.title for embed in informational} == {
        '📏 SPY OPENING RANGE',
        '🟢 SPY ABOVE OPENING RANGE',
    }
    assert len(state['deliveries']) == 1
    assert state['deliveries'][0]['delivery'] == 'sent'
    assert delivery_counts(tmp_path, state['session_date'])['sent'] == 2


@pytest.mark.asyncio
async def test_collection_disabled_poll_makes_no_requests_or_journals(service, monkeypatch):
    monkeypatch.setattr(config, 'COLLECTION_ENABLED', False)
    await service.cog.poll_once()
    assert not service.provider.calls
    assert not service.channel.sent
    assert files(service.directory) == {}
