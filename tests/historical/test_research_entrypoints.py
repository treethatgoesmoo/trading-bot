"""Moved manual CLI surfaces resolve without invoking research or writing data."""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('module', [
    'report_theta_audit', 'collect_spxw_research', 'backtest_momentum_extension',
    'backtest_open_close_momentum', 'backtest_timeframe_context',
    'backtest_extension_score', 'backtest_filters', 'backtest_patterns',
    'backtest_swing', 'analyze_session_thirds', 'tune_weights', 'tune_swing_weights',
])
def test_help_resolves_moved_cli_without_starting_collection(module):
    # These entry points parse --help before any fetch/write. Positional-only
    # tools are intentionally excluded; see research/README.md for their usage.
    result = subprocess.run([sys.executable, '-m', 'research.' + module, '--help'],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout.lower()
