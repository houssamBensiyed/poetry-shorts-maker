import subprocess
import sys
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import create_app
from app.state import RunLock
from app.video_builder import Cancelled, run_ffmpeg


def test_cross_process_lock_released():
    identity = str(uuid.uuid4())
    lock = RunLock(identity)
    assert lock.acquire()
    code = (
        "from app.state import RunLock; l=RunLock("
        + repr(identity)
        + "); print(l.acquire()); l.release()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
    lock.release()
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "True"


def test_api_auth_and_shared_processor(monkeypatch):
    monkeypatch.setenv("VIDEO_BUILDER_API_KEY", "test-only-key")

    class FakeProcessor:
        active = False

        def start(self):
            if self.active:
                return False
            self.active = True
            return True

        def status(self):
            return {"status": "processing" if self.active else "idle", "render_progress": 37}

        def close(self):
            self.active = False

    processor = FakeProcessor()
    with TestClient(create_app(processor=processor)) as client:
        assert client.get("/run").status_code == 401
        assert client.get("/status").status_code == 401
        headers = {"X-API-Key": "test-only-key"}
        assert client.get("/run", headers=headers).status_code == 202
        assert client.get("/run", headers=headers).json()["status"] == "already_running"
        assert client.get("/status", headers=headers).json()["render_progress"] == 37
    assert not processor.active


def test_api_requires_key(monkeypatch):
    monkeypatch.delenv("VIDEO_BUILDER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="VIDEO_BUILDER_API_KEY"):
        create_app()


def test_ffmpeg_cancellation(tmp_path):
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    try:
        with pytest.raises(Cancelled):
            run_ffmpeg(
                ["-re", "-f", "lavfi", "-i", "color=s=64x64:r=10", "-f", "null", "-"],
                tmp_path,
                stop,
                100,
                lambda p: None,
                "Cancellation test",
            )
    finally:
        timer.cancel()


def test_api_shutdown_closes_processor_when_lifespan_raises(monkeypatch):
    import asyncio

    monkeypatch.setenv("VIDEO_BUILDER_API_KEY", "test-only")
    closed = []
    app = create_app(processor=SimpleNamespace(close=lambda: closed.append(True)))

    async def scenario():
        with pytest.raises(RuntimeError, match="lifespan failure"):
            async with app.router.lifespan_context(app):
                raise RuntimeError("lifespan failure")

    asyncio.run(scenario())
    assert closed == [True]


def test_ffmpeg_progress_callback_error_is_not_swallowed(tmp_path):
    def progress(value):
        raise ValueError("Progress observer failed")

    with pytest.raises(ValueError, match="Progress observer failed"):
        run_ffmpeg(
            ["-f", "lavfi", "-i", "color=s=64x64:r=10:d=1", "-f", "null", "-"],
            tmp_path,
            threading.Event(),
            1,
            progress,
            "Progress failure",
        )
