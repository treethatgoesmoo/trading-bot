import subprocess
import sys

import pytest

from core.process_lock import ProcessLock


def test_same_directory_exclusive_and_other_directory_independent(tmp_path):
    first = ProcessLock(tmp_path).acquire()
    try:
        with pytest.raises(RuntimeError, match='Another service'):
            ProcessLock(tmp_path).acquire()
        other = ProcessLock(tmp_path / 'other').acquire()
        other.release()
    finally:
        first.release()
    with ProcessLock(tmp_path):
        pass
    assert (tmp_path / '.service.lock').exists()


def test_child_termination_releases_ownership(tmp_path):
    code = ('import sys,time; from core.process_lock import ProcessLock; '
            'lock=ProcessLock(sys.argv[1]).acquire(); print("ready",flush=True); time.sleep(60)')
    child = subprocess.Popen([sys.executable, '-u', '-c', code, str(tmp_path)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(RuntimeError, match='Another service'):
            ProcessLock(tmp_path).acquire()
    finally:
        child.terminate()
        child.communicate(timeout=10)
    with ProcessLock(tmp_path):
        pass
