"""Small OS file lock shared by TUI/API, keyed by spreadsheet identity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class RunLock:
    def __init__(self, identity):
        digest = hashlib.sha256(identity.encode()).hexdigest()
        directory = Path(tempfile.gettempdir())/'poetry-video-builder-locks'
        directory.mkdir(mode=0o700,exist_ok=True)
        self.path = directory/(digest+'.lock')
        self.state_path = directory/(digest+'.json')
        self.handle = None

    def acquire(self):
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            if os.name == 'nt':
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(self.handle,fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def busy(self):
        other = RunLock.__new__(RunLock)
        other.path, other.handle = self.path, None
        if other.acquire():
            other.release()
            return False
        return True

    def publish(self, data):
        path = self.state_path.with_suffix(f'.{os.getpid()}.tmp')
        path.write_text(json.dumps(data),encoding='utf-8')
        path.replace(self.state_path)

    def external_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError,ValueError):
            return {'status':'processing','stage':'Starting in another process','render_progress':None}


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = dict(status='idle',current_job=None,voiceover='',stage='Ready',render_progress=None,
                         pending=0,completed=0,failed=0,error='')

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.data)
