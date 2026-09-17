"""Real startup, opt-in configuration and retired-feature isolation gates."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
from bot import core
from spy_setup.cog import SpySetupCog

ROOT = Path(__file__).resolve().parents[1]


def read_config(values):
    # Never read the developer's ignored .env or return credentials in output.
    script = '''
import sys, types, os, json
sys.modules['dotenv'] = types.SimpleNamespace(load_dotenv=lambda: None)
for name in list(os.environ):
    if name not in ('SYSTEMROOT', 'PATH', 'WINDIR'):
        del os.environ[name]
os.environ.update(json.loads(sys.argv[1]))
import config
print(json.dumps({name: getattr(config, name) for name in (
    'COLLECTION_ENABLED', 'PUBLICATION_ENABLED', 'SPX_CHANNEL_ID',
    'SPX_POLL_SECONDS', 'THETA_AUDIT_DAYS')}))
'''
    return subprocess.run([sys.executable, '-c', script, json.dumps(values)],
                          cwd=ROOT, capture_output=True, text=True, timeout=20)


def test_default_is_explicitly_off_and_legacy_settings_cannot_break_startup():
    result = read_config({'FORECAST_INTERVAL_MINUTES': 'not-an-integer',
                          'BB_PERIOD': 'broken', 'AUTO_TUNE_DAYS': 'invalid',
                          'SPX_FLOW_POLL_SECONDS': 'invalid', 'SWING_ENTRY_ZONE_ATR_MULT': 'invalid',
                          'NOTIFICATION_MODE': 'legacy', 'ZERO_DTE_ENABLED': '1',
                          'GIT_AUTOCOMMIT_ENABLED': '1', 'ALERT_CHANNEL_ID': '123'})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(COLLECTION_ENABLED=False, PUBLICATION_ENABLED=False,
                                            SPX_CHANNEL_ID=0, SPX_POLL_SECONDS=60, THETA_AUDIT_DAYS=30)


def test_only_current_names_enable_destination_and_poll_settings():
    result = read_config({'COLLECTION_ENABLED': 'TRUE', 'PUBLICATION_ENABLED': '1',
                          'SPX_CONTEXT_CHANNEL_ID': '123', 'SPX_CONTEXT_POLL_SECONDS': '45'})
    assert result.returncode == 0
    settings = json.loads(result.stdout)
    assert settings['COLLECTION_ENABLED'] and settings['PUBLICATION_ENABLED']
    assert settings['SPX_CHANNEL_ID'] == 0 and settings['SPX_POLL_SECONDS'] == 60
    result = read_config({'SPX_CHANNEL_ID': '0', 'SPX_CONTEXT_CHANNEL_ID': 'bad',
                          'SPX_POLL_SECONDS': '60', 'SPX_CONTEXT_POLL_SECONDS': 'bad'})
    assert result.returncode == 0
    assert json.loads(result.stdout)['SPX_CHANNEL_ID'] == 0


@pytest.mark.parametrize('values', [
    {'COLLECTION_ENABLED': 'yes'}, {'PUBLICATION_ENABLED': 'typo'},
    {'SPX_CHANNEL_ID': '-1'}, {'SPX_POLL_SECONDS': '29'}, {'SPX_POLL_SECONDS': '61'},
    {'SPX_CHANNEL_ID': 'private-value-must-not-be-logged'},
])
def test_invalid_live_settings_fail_explicitly_without_echoing_values(values):
    result = read_config(values)
    assert result.returncode != 0
    assert 'ValueError:' in result.stderr
    assert 'private-value-must-not-be-logged' not in result.stderr


def test_invalid_telemetry_duration_disables_metadata_not_service():
    result = read_config({'THETA_AUDIT_DAYS': 'invalid'})
    assert result.returncode == 0
    assert json.loads(result.stdout)['THETA_AUDIT_DAYS'] == 0


@pytest.mark.parametrize('enabled,key,expected_client', [(False, '', False), (False, 'test', False),
                                                       (True, '', False), (True, 'test', True)])
def test_actual_bot_installs_only_spy_setup_and_status(enabled, key, expected_client):
    async def check():
        bot = core.build_bot()
        with patch.multiple(config, COLLECTION_ENABLED=enabled, PUBLICATION_ENABLED=False,
                            THETADATA_API_KEY=key, SPX_CHANNEL_ID=0), \
                patch('core.thetadata_client.ThetaDataClient') as client, \
                patch.object(SpySetupCog, 'cog_load', new=AsyncMock()), \
                patch.object(SpySetupCog, 'cog_unload', new=AsyncMock()):
            try:
                await core.install_service(bot)
                assert set(bot.cogs) == {'SpySetup'}
                assert {command.name for command in bot.commands} == {'status', 'preview', 'replay', 'help', 'day', 'cmd', 'range'}
                assert bot.get_command('bb') is None
                for name in ('audit', 'commit', 'spx', 'dashboard', 'stats', 'swing', 'events'):
                    assert bot.get_command(name) is None
                if expected_client:
                    client.assert_called_once_with(purpose='spy_setup')
                else:
                    client.assert_not_called()
                cog = bot.get_cog('SpySetup')
                assert (cog.theta_client is not None) == expected_client
            finally:
                await bot.close()
    asyncio.run(check())


def test_missing_discord_token_does_not_install_anything():
    with patch.object(config, 'DISCORD_TOKEN', ''), patch.object(core, 'build_bot') as builder:
        with pytest.raises(RuntimeError, match='DISCORD_TOKEN'):
            asyncio.run(core.run_bot())
        builder.assert_not_called()


def test_real_main_import_cannot_reach_historical_packages_or_run_git():
    script = '''
import importlib.abc, sys, types
sys.modules['dotenv'] = types.SimpleNamespace(load_dotenv=lambda: None)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('research', 'zero_dte', 'swing', 'yfinance') or fullname == 'core.git_sync':
            raise AssertionError('Retired import: ' + fullname)
sys.meta_path.insert(0, Guard())
import main, bot.core
assert not any(name.startswith('research') for name in sys.modules)
print('OK')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'OK'


def test_logging_setup_is_explicit_and_writes_only_under_configured_logs(tmp_path):
    import main
    with patch.object(main, 'LOGS_DIR', str(tmp_path)), patch('main.logging.basicConfig') as setup:
        main.configure_logging()
        handlers = setup.call_args.kwargs['handlers']
        try:
            assert Path(handlers[1].baseFilename) == tmp_path / 'bot.log'
            assert handlers[1].backupCount == 30
        finally:
            for handler in handlers:
                handler.close()


def test_retired_source_trees_are_absent():
    for name in ('research', 'market_state', 'swing', 'zero_dte', 'dashboard.html',
                 'core/indicators.py', 'requirements-research.txt'):
        assert not (ROOT / name).exists(), name
