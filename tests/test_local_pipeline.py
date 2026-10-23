"""Exercise local input through the real planner, FFmpeg and completion outbox."""

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.buffer import VoiceBuffer
from app.config import load_config
from app.footage import probe
from app.models import Transcript
from app.processor import Processor
from app.timeline import Word


def local_config(tmp_path):
    config = load_config()
    config["paths"].update(
        buffer=str(tmp_path / "voices"),
        image_buffer=str(tmp_path / "images"),
        temp=str(tmp_path / "temp"),
        output=str(tmp_path / "output"),
    )
    config["video"].update(width=320, height=480, fps=20, preset="ultrafast")
    config["captions"].update(font_size=30, safe_margin=20)
    return config


def run_processor(config):
    processor = Processor(config)
    try:
        assert processor.start()
        processor.thread.join(timeout=20)
        assert not processor.thread.is_alive()
        assert processor.status()["status"] == "idle", processor.status()
        return processor.status()
    finally:
        processor.close()


def test_local_voices_render_and_are_consumed_without_table_or_publisher(
    monkeypatch, tmp_path, file_server
):
    config = local_config(tmp_path)
    for name in ("BASEROW_API_TOKEN", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WHISPER_API_KEY", "test-transcription-only")

    def forbidden(*args, **kwargs):
        raise AssertionError("Local rendering must not use external queue/publishing/downloads")

    monkeypatch.setattr("app.jobs.JobTable.__init__", forbidden)
    monkeypatch.setattr("app.uploads.Uploader.__init__", forbidden)
    monkeypatch.setattr("app.downloads.Downloader.download", forbidden)
    seen = []

    def transcribe(audio, *args):
        seen.append(audio.name)
        return Transcript((Word("Hello.", 0.1, 0.7),), ())

    monkeypatch.setattr(
        "app.bootstrap.Transcriber", lambda config: SimpleNamespace(transcribe=transcribe)
    )
    image_dir = Path(config["paths"]["image_buffer"])
    image_dir.mkdir()
    for n, color in enumerate(("blue", "red", "green")):
        Image.new("RGB", (160, 120), color).save(image_dir / f"image-{n}.png")
    source = tmp_path / "recording.wav"
    source.write_bytes(file_server[1])
    buffer = VoiceBuffer(config["paths"]["buffer"])
    first, second = buffer.add(source), buffer.add(source)

    status = run_processor(config)
    assert status["completed"] == 2 and status["failed"] == 0
    assert seen == ["voice-000001.wav", "voice-000002.wav"]
    assert not first.exists() and not second.exists()
    assert source.exists(), "Ingestion must preserve the user's original file"
    assert len(list(image_dir.glob("*.png"))) == 3, "Images remain reusable"
    events = buffer.events()
    assert [event["voice_id"] for event in events] == ["voice-000001", "voice-000002"]
    for event in events:
        assert event["state"] == "consumed"
        output = Path(event["output"])
        media = probe(output)
        assert {s["codec_type"] for s in media["streams"]} == {"video", "audio"}
        assert abs(float(media["format"]["duration"]) - 1) < 0.05
    run_processor(config)
    assert len(seen) == 2, "Restart must not rerender consumed voices"
    assert buffer.add(source).name == "voice-000003.wav"
