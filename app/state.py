"""Coordinate local process ownership and thread-safe display snapshots.

The dashboard and API derive lock paths from the same queue identity. Kernel
file locks provide exclusion and a separate JSON file provides best-effort
progress for observers. This coordinates processes sharing the same filesystem
lock directory, including the configured Docker volume; it is not a distributed
lock across computers. Display snapshots do not replace per-job recovery files.
"""

import errno
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class RunLock:
    """Hold an OS file lock for one queue and publish its owner's display state.

    The open file handle represents ownership. Lock files may remain on disk
    after release; existence alone does not indicate that a run is active.
    """

    def __init__(self, identity):
        """Hash a stable queue identity into paths in the shared temporary directory.

        Hashing avoids putting URL characters into filenames. It is a naming scheme,
        not a secret store. Create the directory now and acquire ownership later.
        """
        digest = hashlib.sha256(identity.encode()).hexdigest()
        directory = Path(tempfile.gettempdir()) / "poetry-video-builder-locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / (digest + ".lock")
        self.state_path = directory / (digest + ".json")
        self.handle = None

    def acquire(self):
        """Attempt a nonblocking exclusive lock and return whether it was acquired.

        Retain the open handle until release. Windows locks a seeded byte; POSIX uses
        flock. Return False for normal contention or reuse of an already held object;
        propagate unexpected filesystem/locking errors instead of hiding them.
        """
        if self.handle is not None:
            return False
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        self.handle = handle
        return True

    def release(self):
        """Unlock and close the ownership handle; do nothing when already released.

        Close the handle even if explicit unlock fails so OS resources are reclaimed.
        The lock and last-status files remain available for future runs.
        """
        if self.handle:
            handle, self.handle = self.handle, None
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def busy(self):
        """Probe ownership with a separate nonblocking handle.

        Release a successfully acquired probe immediately. This is a momentary busy
        check for display/refresh; start() must still acquire its own lock to own work.
        """
        other = RunLock.__new__(RunLock)
        other.path, other.handle = self.path, None
        if other.acquire():
            other.release()
            return False
        return True

    def publish(self, data):
        """Atomically replace shared JSON display state using a PID-specific temporary file.

        The caller serializes publication and holds queue ownership. Errors propagate
        for Processor.update to isolate; no fsync or power-loss durability is promised.
        """
        path = self.state_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
            path.replace(self.state_path)
        finally:
            path.unlink(missing_ok=True)

    def external_state(self):
        """Read a plausible external snapshot or return a minimal starting state.

        Missing, malformed or invalid-status files are expected during startup and
        should not break a dashboard observing another lock holder. Additional fields
        are passed through, so consumers use defaults for optional display values.
        """
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("status") not in (
                "idle",
                "processing",
                "error",
            ):
                raise ValueError("Invalid shared state")
            return data
        except (OSError, ValueError):
            return {
                "status": "processing",
                "stage": "Starting in another process",
                "render_progress": None,
            }


class State:
    """Protect the worker's display fields with a reentrant thread lock.

    This is in-memory observability, not the authoritative remote job repository.
    Snapshots are shallow copies; current fields are scalar display values.
    """

    def __init__(self):
        """Initialize idle status, empty job details and zero queue counters."""
        self.lock = threading.RLock()
        self.data = dict(
            status="idle",
            current_job=None,
            voiceover="",
            stage="Ready",
            render_progress=None,
            pending=0,
            completed=0,
            failed=0,
            error="",
        )

    def update(self, **values):
        """Merge supplied fields while holding the state lock.

        Partial updates let media callbacks report stage/progress without replacing
        counters and other coordinator-owned display fields.
        """
        with self.lock:
            self.data.update(values)

    def snapshot(self):
        """Return a shallow dictionary copy so readers cannot mutate stored fields."""
        with self.lock:
            return dict(self.data)
