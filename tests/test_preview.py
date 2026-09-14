"""Preview uses the real renderer but cannot access provider/evidence state."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

import config
from spy_setup.cog import SpySetupCog
from spy_setup.preview import DEMO, preview_embeds


def test_examples_are_labeled_deterministic_and_within_discord_limits():
    embeds = preview_embeds()
    assert [e.to_dict() for e in embeds] == [e.to_dict() for e in preview_embeds()]
    assert len(embeds) == 5
    assert sum(len(e) for e in embeds) <= 6000
    for embed in embeds:
        assert DEMO in embed.title and DEMO in embed.footer.text
        assert 'NOT current market data' in embed.description
        assert len(embed.title) <= 256
        assert any('NOT SPX prices' in f.name for f in embed.fields)
    assert 'not set' in embeds[0].fields[0].value
    assert '600.92' in embeds[1].fields[0].value


@pytest.mark.asyncio
@pytest.mark.parametrize('channel,allowed', [(123, True), (456, False)])
async def test_preview_private_and_independent_of_live_service(channel, allowed):
    # No constructed collector, client, store or state: access to any would fail.
    service = SimpleNamespace(_private=SpySetupCog._private)
    ctx = SimpleNamespace(channel=SimpleNamespace(id=channel), send=AsyncMock())
    with patch.object(config, 'SPX_CHANNEL_ID', 123), \
         patch.object(config, 'COLLECTION_ENABLED', False), \
         patch.object(config, 'PUBLICATION_ENABLED', False):
        await SpySetupCog.preview.callback(service, ctx)
    ctx.send.assert_awaited_once()
    kwargs = ctx.send.call_args.kwargs
    if allowed:
        assert DEMO in kwargs['content']
        assert len(kwargs['embeds']) == 5
    else:
        assert not kwargs
        assert 'private channel' in ctx.send.call_args.args[0]
