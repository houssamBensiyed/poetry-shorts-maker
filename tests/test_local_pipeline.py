"""Exercise local input through the real planner, FFmpeg and completion outbox."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import create_app
from app.buffer import VoiceBuffer
from app.config import load_config
from app.footage import probe
from app.models import Transcript
from app.processor import Processor
from app.timeline import Word


def local_config(tmp_path):
    config = load_config()
    config["composition"]["images_directory"] = str(tmp_path / "assets" / "images")
    config["paths"].update(
        buffer=str(tmp_path / "voices"),
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
    image_dir = Path(config["composition"]["images_directory"])
    image_dir.mkdir(parents=True)
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


def test_missing_shared_images_keeps_voice_and_never_falls_back_to_providers(
    monkeypatch, tmp_path, file_server
):
    config = local_config(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Missing assets must not trigger remote import or transcription")

    monkeypatch.setattr("app.jobs.JobTable.__init__", forbidden)
    monkeypatch.setattr("app.uploads.Uploader.__init__", forbidden)
    monkeypatch.setattr("app.bootstrap.Transcriber", forbidden)
    source = tmp_path / "recording.wav"
    source.write_bytes(file_server[1])
    buffer = VoiceBuffer(config["paths"]["buffer"])
    voice = buffer.add(source)
    processor = Processor(config)
    try:
        assert processor.start()
        processor.thread.join(timeout=10)
        assert not processor.active
        assert processor.status()["status"] == "error"
        assert "composition.images_directory" in processor.status()["error"]
        assert voice.read_bytes() == file_server[1]
        assert buffer.events() == []
        assert not Path(config["composition"]["images_directory"]).exists()
    finally:
        processor.close()


@pytest.mark.parametrize("mode", ["image", "video", "hybrid"])
def test_api_mode_reaches_real_renderer_and_keeps_assets(
    monkeypatch, tmp_path, file_server, video_asset, mode
):
    config = local_config(tmp_path)
    config["composition"]["videos_directory"] = str(video_asset.parent)
    if mode != "video":
        directory = Path(config["composition"]["images_directory"])
        directory.mkdir(parents=True)
        Image.new("RGB", (80, 120), "blue").save(directory / "image.png")
    original = video_asset.read_bytes()
    monkeypatch.setenv("VIDEO_BUILDER_API_KEY", "test-only")
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")
    monkeypatch.setattr(
        "app.bootstrap.Transcriber",
        lambda config: SimpleNamespace(
            transcribe=lambda *args: Transcript(
                tuple(
                    Word(word, start + j * 0.04, start + j * 0.04 + 0.02)
                    for start in (0, 0.35, 0.7)
                    for j, word in enumerate("One two three.".split())
                ),
                (),
            )
        ),
    )
    from app.video_builder import render

    plans = []
    channel_labels = []

    def record(plan, *args):
        plans.append(plan)
        output = render(plan, *args)
        channel_labels.append((Path(args[2]) / "captions.ass").read_text())
        return output

    monkeypatch.setattr("app.bootstrap.render", record)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(file_server[1])
    buffer = VoiceBuffer(config["paths"]["buffer"])
    voice = buffer.add(audio)
    processor = Processor(config)
    with TestClient(create_app(config, processor)) as client:
        headers = {"X-API-Key": "test-only"}
        assert client.get("/run?mode=invalid", headers=headers).status_code == 422
        assert voice.exists() and not processor.active
        assert (
            client.get("/run", params={"channel_name": "x" * 81}, headers=headers).status_code
            == 422
        )
        assert (
            client.get(
                "/run", params={"mode": mode, "channel_name": "My Channel"}, headers=headers
            ).status_code
            == 202
        )
        processor.thread.join(timeout=20)
        assert not processor.active
        status = client.get("/status", headers=headers).json()
        assert status["completed"] == 1 and status["failed"] == 0, status
        assert status["mode"] == mode
        assert status["channel_name"] == "My Channel"
        assert processor.config["captions"]["channel_name"] == ""
        assert processor.config["footage"]["mode"] == "image", "Overrides are per-run"
    kinds = [interval.source.kind for interval in plans[0].footage]
    assert kinds == (["video", "image", "video"] if mode == "hybrid" else [mode] * 3)
    assert not voice.exists() and video_asset.read_bytes() == original
    assert ",Channel," in channel_labels[0] and "My Channel" in channel_labels[0]
    output = Path(buffer.events()[0]["output"])
    assert abs(float(probe(output)["format"]["duration"]) - 1) < 0.05
