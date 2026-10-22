import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LiveLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=500)
        self.sequence = 0
        self.guard = threading.Lock()

    def emit(self, record):
        with self.guard:
            self.sequence += 1
            self.lines.append((self.sequence, self.format(record)))

    def since(self, sequence):
        with self.guard:
            return [(n, line) for n, line in self.lines if n > sequence]


def configure(directory):
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
