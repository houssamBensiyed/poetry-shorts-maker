import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from textual.widgets import Button, RichLog, Select

from app.buffer import VoiceBuffer
from app.cancellation import check_cancel
from app.config import load_config
from app.dashboard import Dashboard
from app.logger import configure
from app.models import Transcript
from app.processor import Processor
from app.timeline import Word


def local_config(tmp_path):
    config = load_config()
    config["composition"]["images_directory"] = str(tmp_path / "assets" / "images")
    config["paths"].update(
        buffer=str(tmp_path / "voices"),
        temp=str(tmp_path / "work"),
        output=str(tmp_path / "output"),
    )
    config["video"].update(width=180, height=320, fps=20, preset="ultrafast")
    config["captions"].update(font_size=18, safe_margin=12)
    return config


async def finish_run(processor, pilot):
    assert processor.thread is not None
    await asyncio.to_thread(processor.thread.join, 15)
    assert not processor.active, processor.status()
    await pilot.pause(0.25)


def test_local_choice_rejects_empty_buffer_without_using_providers(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Empty local processing must not use a provider")

    monkeypatch.setattr("app.automations.BufferAutomations.__init__", forbidden)

    async def scenario():
        c = local_config(tmp_path)
        logs = configure(tmp_path)
        p = Processor(c)
        app = Dashboard(p, logs)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert app.query_one("#source", Select).value == "local"
            assert app.query_one("#start", Button).disabled is False
            assert app.query_one("#logs", RichLog).lines
            await pilot.click("#start")
            await finish_run(p, pilot)
            assert p.status()["status"] == "error"
            assert p.status()["pending"] == 0
            assert "buffer is empty" in p.status()["error"]
            assert "Add voice files" in p.status()["error"] and "Baserow" in p.status()["error"]
            assert not p.run_lock.busy()
            assert app.query_one("#start", Button).disabled is False
            await pilot.click("#exit")
        p.close()

    asyncio.run(scenario())


def test_local_choice_renders_existing_voice_without_importing(tmp_path, monkeypatch, file_server):
    c = local_config(tmp_path)
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")
    monkeypatch.setattr(
        "app.bootstrap.Transcriber",
        lambda config: SimpleNamespace(
            transcribe=lambda *args: Transcript((Word("Hello.", 0.1, 0.7),), ())
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Local choice must not import from the sheet or publish")

    monkeypatch.setattr("app.automations.BufferAutomations.__init__", forbidden)
    image_dir = Path(c["composition"]["images_directory"])
    image_dir.mkdir(parents=True)
    Image.new("RGB", (80, 120), "blue").save(image_dir / "image.png")
    source = tmp_path / "recording.wav"
    source.write_bytes(file_server[1])
    buffer = VoiceBuffer(c["paths"]["buffer"])
    voice = buffer.add(source)

    async def scenario():
        p = Processor(c)
        try:
            async with Dashboard(p, configure(tmp_path)).run_test(size=(80, 30)) as pilot:
                await pilot.click("#start")
                await finish_run(p, pilot)
                assert p.status()["completed"] == 1 and p.status()["failed"] == 0, p.status()
                assert not voice.exists()
                assert Path(buffer.events()[0]["output"]).is_file()
                await pilot.click("#start")
                await finish_run(p, pilot)
                assert p.status()["status"] == "error"
                assert "buffer is empty" in p.status()["error"]
                assert p.status()["completed"] == 1
                await pilot.click("#exit")
        finally:
            p.close()

    asyncio.run(scenario())


def test_selecting_baserow_imports_then_renders_in_the_same_run(tmp_path, monkeypatch, file_server):
    c = local_config(tmp_path)
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")
    monkeypatch.setattr(
        "app.bootstrap.Transcriber",
        lambda config: SimpleNamespace(
            transcribe=lambda *args: Transcript((Word("Hello.", 0.1, 0.7),), ())
        ),
    )
    source = tmp_path / "remote-recording.wav"
    source.write_bytes(file_server[1])
    calls = []
    image_dir = Path(c["composition"]["images_directory"])
    image_dir.mkdir(parents=True)
    Image.new("RGB", (80, 120), "blue").save(image_dir / "image.png")

    def refill(service):
        calls.append(service.stop)
        assert not any(job.status == "NOT_PROCESSED" for job in service.buffer.jobs())
        service.buffer.add(source)
        return 1

    monkeypatch.setattr("app.automations.BufferAutomations.refill", refill)

    async def scenario():
        p = Processor(c)
        app = Dashboard(p, configure(tmp_path))
        try:
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.click("#source")
                await pilot.press("end", "enter")
                assert app.query_one("#source", Select).value == "baserow"
                await pilot.click("#start")
                await finish_run(p, pilot)
                assert calls == [p.stop_event]
                assert p.status()["completed"] == 1 and p.status()["failed"] == 0, p.status()
                await pilot.click("#exit")
        finally:
            p.close()

    asyncio.run(scenario())


def test_baserow_import_can_be_stopped_and_source_is_locked_while_busy(tmp_path, monkeypatch):
    entered = threading.Event()

    def refill(service):
        entered.set()
        assert service.stop.wait(5)
        check_cancel(service.stop)

    monkeypatch.setattr("app.automations.BufferAutomations.refill", refill)

    async def scenario():
        p = Processor(local_config(tmp_path))
        app = Dashboard(p, configure(tmp_path))
        try:
            async with app.run_test(size=(80, 30)) as pilot:
                app.query_one("#source", Select).value = "baserow"
                await pilot.click("#start")
                assert await asyncio.to_thread(entered.wait, 2)
                await pilot.pause()
                assert app.query_one("#source", Select).disabled
                assert app.query_one("#start", Button).disabled
                assert not app.query_one("#stop", Button).disabled
                assert p.run_lock.busy() and not p.start()
                await pilot.click("#stop")
                await finish_run(p, pilot)
                assert p.status()["stage"] == "Stopped" and not p.run_lock.busy()
                assert not app.query_one("#source", Select).disabled
                await pilot.click("#exit")
        finally:
            p.close()

    asyncio.run(scenario())
