"""Cheap commit detection on a persistent, read-only registry connection.

Each consumer retains its own token, captured BEFORE reading its snapshot. A
commit during that read must still be detected on the consumer's next check.
No transaction or connection lock escapes ``version``.
"""
from pathlib import Path
import threading

from gateway.sqlite_startup import connect_readonly


class RegistryVersion:
    def __init__(self, path):
        self.path = Path(path)
        self._connection = None
        self._identity = None
        self._epoch = 0
        self._lock = threading.Lock()

    def version(self):
        with self._lock:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._connection is None or identity != self._identity:
                self.close()
                self._connection = connect_readonly(self.path, check_same_thread=False, timeout=0)
                self._identity = identity
                self._epoch += 1
            return self._epoch, self._connection.execute('PRAGMA data_version').fetchone()[0]

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __del__(self):
        self.close()
