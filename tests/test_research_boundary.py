"""Retirement boundaries, runnable without optional research dependencies."""
import ast
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_retired_runtime_files_are_deleted_not_compatibility_wrappers():
    for name in ('bot/shared_cog.py', 'core/git_sync.py', 'core/economic_events.py',
                 'zero_dte/cog.py', 'zero_dte/bb_signal_cog.py',
                 'zero_dte/spx_context_cog.py', 'zero_dte/flow_cog.py',
                 'zero_dte/audit_cog.py', 'zero_dte/bb_watcher.py',
                 'zero_dte/volume_watcher.py', 'swing/cog.py', 'swing/options.py'):
        assert not (ROOT / name).exists(), name


def test_new_runtime_imports_without_research_or_yahoo_packages():
    # This checks the new runtime dependency closure, not bot/core assembly,
    # which is covered separately by the integration owner's contract tests.
    code = '''
import importlib.abc
import sys
class RejectHistorical(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'research', 'zero_dte', 'swing', 'yfinance'}:
            raise AssertionError('Historical dependency reached: ' + fullname)
sys.meta_path.insert(0, RejectHistorical())
import market_state.cog
import core.state_store
import core.session_bars
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_research_configuration_does_not_import_live_configuration():
    code = '''
import importlib.abc
import sys
class RejectLiveConfig(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'config':
            raise AssertionError('Research imported live config')
sys.meta_path.insert(0, RejectLiveConfig())
import research.config
assert not hasattr(research.config, 'DISCORD_TOKEN')
assert not hasattr(research.config, 'GIT_AUTOCOMMIT_ENABLED')
assert not hasattr(research.config, 'BB_WATCH_ENABLED')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_research_has_no_cogs_or_imports_of_old_package_paths():
    retired_prefixes = ('zero_dte', 'swing', 'core.analytics', 'core.context_audit_report',
                        'core.market_data', 'core.rate_limiter', 'core.tracking',
                        'core.tuning', 'core.vix', 'core.git_sync')
    for path in (ROOT / 'research').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules = ([node.module] if isinstance(node, ast.ImportFrom) else
                       [n.name for n in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                assert module and module != 'config', (path, module)
                assert not any(module == p or module.startswith(p + '.')
                               for p in retired_prefixes), (path, module)
                assert not module.startswith('discord'), (path, module)


def test_historical_models_do_not_keep_notification_or_position_adapters():
    removed = {
        'research/zero_dte/strategy.py': {'StrategyState', 'forecast_post_reason'},
        'research/zero_dte/tracker.py': {'log_user_vote'},
        'research/swing/strategy.py': {'SwingSignal', 'entry_zone', 'build_signal',
                                      'scan_ticker', 'check_price_vs_signal'},
    }
    for path, symbols in removed.items():
        tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
        names = {node.name for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not names.intersection(symbols), path


def test_dependencies_and_make_targets_separate_research():
    live = (ROOT / 'requirements.txt').read_text(encoding='utf-8').lower()
    research = (ROOT / 'requirements-research.txt').read_text(encoding='utf-8').lower()
    assert 'yfinance' not in live
    assert 'yfinance' in research
    makefile = (ROOT / 'Makefile').read_text(encoding='utf-8')
    assert '--ignore=tests/historical' in makefile
    assert 'backtest-swing:' not in makefile
    assert 'rm -rf __pycache__ core/__pycache__ zero_dte/__pycache__ swing/__pycache__ bot/__pycache__ .bot.pid' not in makefile


def test_moved_tools_keep_shared_evidence_paths():
    from core.paths import DATA_DIR, ROOT as CODE_ROOT, THETADATA_DATA_DIR
    assert Path(CODE_ROOT) == ROOT
    assert Path(DATA_DIR) == ROOT / 'data'
    assert Path(THETADATA_DATA_DIR) == ROOT / '.thetadata-data'
    for module in ('backfill', 'dashboard', 'report_theta_audit', 'collect_spxw_research',
                   'backtest_swing', 'tune_weights', 'tune_swing_weights'):
        assert (ROOT / 'research' / (module + '.py')).is_file()
        assert not (ROOT / (module + '.py')).exists()
