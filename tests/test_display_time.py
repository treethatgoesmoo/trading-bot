"""Display-zone conversion must not change strategy timestamps."""
from spy_setup.embeds import display_time, snapshot_embed
from spy_setup.preview import preview_embeds


def test_pacific_daylight_and_standard_time():
    assert display_time('2026-09-14T14:27:00+00:00') == '07:27 AM PDT'
    assert display_time('2026-01-06T15:27:00+00:00') == '07:27 AM PST'
    assert display_time('2026-09-14T14:29:00+00:00', include_date=True) == '2026-09-14 07:29:00 AM PDT'
    assert display_time(None) == 'not set'


def test_embed_converts_all_times_without_mutating_snapshot():
    import copy
    snapshot = {'fresh': True, 'classification': {
        'available': True, 'direction': 'bearish', 'lifecycle': 'forming',
        'setup_id': 'example', 'bar_at': '2026-09-14T14:26:00+00:00',
        'event_at': '2026-09-14T14:27:00+00:00',
        'expires_at': '2026-09-14T14:42:00+00:00'}}
    original = copy.deepcopy(snapshot)
    embed = snapshot_embed(snapshot)
    assert '07:27 AM PDT' in embed.description
    assert '07:27 AM PDT' in embed.fields[0].value
    assert '07:42 AM PDT' in embed.fields[0].value
    assert snapshot == original
    assert all('PST' in e.fields[0].value for e in preview_embeds())
