import json
import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.buffer import BufferRunner, VoiceBuffer
from app.cancellation import Cancelled


def test_numbering_includes_manual_entries_and_rejects_duplicate_ids(tmp_path):
    buffer = VoiceBuffer(tmp_path / "voices")
    buffer.directory.mkdir()
    (buffer.directory / "voice-000009.mp3").write_bytes(b"audio")
    source = tmp_path / "voice.wav"
    source.write_bytes(b"audio")
    assert buffer.add(source).name == "voice-000010.wav"
    assert [job.row for job in buffer.jobs()] == [9, 10]
    (buffer.directory / "voice-000009.wav").write_bytes(b"duplicate")
    with pytest.raises(ValueError, match="Duplicate"):
        buffer.jobs()


@pytest.mark.parametrize("name", ["poem.wav", "voice-000000.wav", "voice-0000001.wav"])
def test_strict_filenames(tmp_path, name):
    (tmp_path / name).write_bytes(b"audio")
    with pytest.raises(ValueError):
        VoiceBuffer(tmp_path).jobs()


def test_interruption_after_receipt_recovers_without_rendering(monkeypatch, tmp_path):
    buffer = VoiceBuffer(tmp_path / "voices")
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    voice = buffer.add(source)
    job = buffer.jobs()[0]
    output = tmp_path / "output.mp4"
    output.write_bytes(b"validated video")
    consume = buffer._consume

    def interrupted(event):
        raise OSError("interrupted before removal")

    monkeypatch.setattr(buffer, "_consume", interrupted)
    with pytest.raises(OSError):
        buffer.complete(job, output)
    assert voice.exists() and buffer.events()[0]["state"] == "rendered"
    monkeypatch.setattr(buffer, "_consume", consume)
    assert buffer.jobs()[0].status == "DONE"
    assert not voice.exists() and buffer.events()[0]["state"] == "consumed"


@pytest.mark.parametrize("failure", [RuntimeError("render failed"), Cancelled("stopped")])
def test_render_failure_or_cancellation_retains_voice_and_emits_no_event(tmp_path, failure):
    buffer = VoiceBuffer(tmp_path / "voices")
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    voice = buffer.add(source)
    job = buffer.jobs()[0]

    def fail(*args):
        raise failure

    runner = BufferRunner(
        buffer,
        SimpleNamespace(run_local=fail),
        {"temp": str(tmp_path / "work"), "output": str(tmp_path), "image_buffer": str(tmp_path)},
        Event(),
        lambda **kwargs: None,
        lambda: None,
    )
    assert not runner.run(job, buffer, [])
    assert voice.exists() and buffer.events() == []


def test_copy_failure_does_not_expose_partial_voice_or_reuse_sequence(monkeypatch, tmp_path):
    buffer = VoiceBuffer(tmp_path / "voices")
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")

    def partial(source, target):
        Path(target).write_bytes(b"partial")
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("app.buffer.shutil.copyfile", partial)
        with pytest.raises(OSError):
            buffer.add(source)
    assert buffer.jobs() == []
    assert buffer.add(source).name == "voice-000002.wav"
    assert json.loads((buffer.metadata / "sequence.json").read_text()) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX service shutdown signal")
def test_empty_worker_cli_needs_no_providers_and_watch_stops_cleanly(tmp_path):
    config = tmp_path / "local.yaml"
    config.write_text("{}")
    command = [sys.executable, "-m", "app.buffer", "run", "--config", str(config)]
    once = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert once.returncode == 0, once.stderr
    assert json.loads(once.stdout)["pending"] == 0
    process = subprocess.Popen(command + ["--watch"], stdout=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Worker did not complete its first poll"
        assert json.loads(process.stdout.readline())["status"] == "idle"
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stdout.close()
