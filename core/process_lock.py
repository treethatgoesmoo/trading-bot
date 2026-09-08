"""Single-machine evidence ownership. Never unlink the stable lock inode."""
import os
from pathlib import Path


class ProcessLock:
    def __init__(self, directory):
        self.path = Path(directory) / '.service.lock'
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            raise RuntimeError('Evidence ownership already acquired')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b'\0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('Another service owns the evidence directory; startup refused') from None
        self._handle = handle
        return self

    def release(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            # Closing releases the OS lock, including on process termination.
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()
