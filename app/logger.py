"""Configure persistent diagnostics and bounded live logs for the dashboard.

Both handlers share formatting and the video_builder logger. Disk logs retain
debug details with rotation, while a thread-safe sequence buffer exposes INFO
and higher messages incrementally to the UI without growing memory indefinitely.
"""

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LiveLog(logging.Handler):
    """Keep the latest 500 formatted records with monotonic sequence numbers.

    Sequence IDs let the dashboard request only unseen lines, including when
    the bounded deque has already discarded older messages.
    """

    def __init__(self):
        """Initialize an empty bounded buffer and its cross-thread guard."""
        super().__init__()
        self.lines = deque(maxlen=500)
        self.sequence = 0
        self.guard = threading.Lock()

    def emit(self, record):
        """Format and append a logging record atomically with its next sequence ID."""
        with self.guard:
            self.sequence += 1
            self.lines.append((self.sequence, self.format(record)))

    def since(self, sequence):
        """Return buffered (sequence, text) pairs newer than the supplied cursor.

        Return a detached list under the guard so UI iteration does not race with
        worker logging. Records already evicted from the deque cannot be replayed.
        """
        with self.guard:
            return [(n, line) for n, line in self.lines if n > sequence]


def configure(directory):
    """Return a live handler after replacing this application's log handlers.

    Create directory as needed and attach a UTF-8 rotating file capped at
    5,000,000 bytes with three backups, plus an INFO-level LiveLog. Disable
    propagation to avoid duplicate root-logger output in the terminal UI. Closing
    old handlers makes repeated application/test setup release file resources.
    """
    logger = logging.getLogger("video_builder")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    Path(directory).mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    disk = RotatingFileHandler(
        Path(directory) / "video_builder.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    disk.setFormatter(formatter)
    live = LiveLog()
    live.setLevel(logging.INFO)
    live.setFormatter(formatter)
    logger.addHandler(disk)
    logger.addHandler(live)
    return live
