import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.automations import BufferAutomations
from app.buffer import VoiceBuffer
from app.config import load_config
from app.models import Job, Transcript
from app.processor import Processor
from app.state import RunLock
from app.timeline import Word


def setup(tmp_path):
    config = load_config()
    config["composition"]["images_directory"] = str(tmp_path / "assets" / "images")
    config["baserow"].update(table_id=51, images_table_id=0)
    config["paths"].update(
        buffer=str(tmp_path / "voices"),
        temp=str(tmp_path / "temp"),
        output=str(tmp_path / "output"),
    )
    return config


def completed(config, tmp_path, row=92):
    buffer = VoiceBuffer(config["paths"]["buffer"])
    audio = tmp_path / f"source-{row}.wav"
    audio.write_bytes(b"audio")
    voice = buffer.add(
        audio,
        origin={
            "provider": "baserow",
            "api_url": config["baserow"]["api_url"],
            "table_id": 51,
            "row_id": row,
            "job_id": "duplicate-display-label",
        },
    )
    job = next(job for job in buffer.jobs() if job.voice_link == str(voice))
    output = tmp_path / f"{voice.stem}.mp4"
    output.write_bytes(b"validated video")
    buffer.complete(job, output)
    return buffer, voice, output


def test_listener_uploads_before_done_and_retries_sheet_without_reupload(tmp_path):
    config = setup(tmp_path)
    buffer, voice, output = completed(config, tmp_path)
    calls = []
    fail_update = True

    def upload(path, progress):
        assert not voice.exists(), "Event follows successful voice consumption"
        assert Path(path) == output
        calls.append("upload")
        return {
            "url": "https://res.cloudinary.com/test/reel.mp4",
            "public_id": voice.stem,
            "bytes": 15,
        }

    def update(job, status, **values):
        calls.append("sheet")
        assert calls[0] == "upload"
        assert job.row == 92 and job.row != 1, "Match original row, never the buffer sequence"
        assert status == "DONE"
        assert values["uploaded_url"] == "https://res.cloudinary.com/test/reel.mp4"
        if fail_update:
            raise RuntimeError("sheet unavailable")

    service = BufferAutomations(
        config,
        table=lambda: SimpleNamespace(update=update),
        publisher=lambda: SimpleNamespace(public_id=lambda path: path.stem, upload=upload),
    )
    with pytest.raises(RuntimeError, match="sheet unavailable"):
        service.deliver()
    assert output.exists() and len(buffer.events()) == 1
    fail_update = False
    assert service.deliver() == 1
    assert service.deliver() == 0
    assert calls == ["upload", "sheet", "sheet"]


def test_ambiguous_upload_is_preserved_for_reconciliation(tmp_path):
    config = setup(tmp_path)
    _, voice, output = completed(config, tmp_path)
    calls = []

    def ambiguous(*args):
        calls.append("upload")
        raise RuntimeError("connection lost after upload")

    service = BufferAutomations(
        config,
        table=lambda: pytest.fail("Must not mark sheet DONE after failed upload"),
        publisher=lambda: SimpleNamespace(public_id=lambda path: path.stem, upload=ambiguous),
    )
    with pytest.raises(RuntimeError, match="connection lost"):
        service.deliver()
    with pytest.raises(RuntimeError, match="Reconcile Cloudinary"):
        service.deliver()
    assert calls == ["upload"] and output.exists() and not voice.exists()
    receipt = json.loads(next(service.delivery.glob("*.json")).read_text())
    assert receipt["state"] == "uploading" and receipt["row_id"] == 92


def test_refill_does_not_contact_provider_with_pending_voice(tmp_path):
    config = setup(tmp_path)
    buffer = VoiceBuffer(config["paths"]["buffer"])
    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    buffer.add(source)
    service = BufferAutomations(config, table=lambda: pytest.fail("Buffer is not empty"))
    assert service.refill() == 0


def test_explicit_import_rejects_existing_local_voices_before_contacting_sheet(tmp_path):
    config = setup(tmp_path)
    source = tmp_path / "recording.wav"
    source.write_bytes(b"audio")
    buffer = VoiceBuffer(config["paths"]["buffer"])
    voice = buffer.add(source)
    service = BufferAutomations(config, table=lambda: pytest.fail("Process local voices first"))
    with pytest.raises(ValueError, match="already contains voices"):
        service.import_for_processing()
    assert voice.exists()


def test_explicit_import_reports_no_ready_rows_and_releases_automation_lock(tmp_path):
    config = setup(tmp_path)
    service = BufferAutomations(config, table=lambda: SimpleNamespace(jobs=lambda: []))
    with pytest.raises(ValueError, match="No new NOT_PROCESSED"):
        service.import_for_processing()
    lock = RunLock(service.buffer.identity + ":automations")
    assert lock.acquire()
    lock.release()


def test_explicit_import_cannot_overlap_the_polling_listener(tmp_path):
    service = BufferAutomations(setup(tmp_path), table=lambda: pytest.fail("Already importing"))
    lock = RunLock(service.buffer.identity + ":automations")
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="automation is busy"):
            service.import_for_processing()
        assert lock.busy()
    finally:
        lock.release()


def test_unmapped_local_voice_does_not_trigger_upload_or_sheet_update(tmp_path):
    config = setup(tmp_path)
    buffer = VoiceBuffer(config["paths"]["buffer"])
    source = tmp_path / "local.wav"
    source.write_bytes(b"audio")
    buffer.add(source)
    output = tmp_path / "reel.mp4"
    output.write_bytes(b"video")
    buffer.complete(buffer.jobs()[0], output)
    service = BufferAutomations(
        config,
        table=lambda: pytest.fail("Local voice has no sheet row"),
        publisher=lambda: pytest.fail("Local voice does not enable publishing"),
    )
    assert service.deliver() == 0


def test_changed_table_cannot_receive_an_old_completion(tmp_path):
    config = setup(tmp_path)
    completed(config, tmp_path)
    config["baserow"]["table_id"] = 999
    service = BufferAutomations(
        config,
        table=lambda: pytest.fail("Must not write to a different table"),
        publisher=lambda: pytest.fail("Must preserve original target before uploading"),
    )
    with pytest.raises(ValueError, match="target changed"):
        service.deliver()


def test_one_failed_delivery_does_not_block_another_voice(tmp_path):
    config = setup(tmp_path)
    completed(config, tmp_path, row=92)
    completed(config, tmp_path, row=93)
    updates = []

    def upload(path, progress):
        if path.stem == "voice-000001":
            raise RuntimeError("first upload unavailable")
        return {
            "url": "https://res.cloudinary.com/test/reel.mp4",
            "public_id": path.stem,
            "bytes": 15,
        }

    service = BufferAutomations(
        config,
        table=lambda: SimpleNamespace(update=lambda job, *args, **kwargs: updates.append(job.row)),
        publisher=lambda: SimpleNamespace(public_id=lambda path: path.stem, upload=upload),
    )
    with pytest.raises(RuntimeError, match="first upload unavailable"):
        service.deliver()
    assert updates == [93]


def test_refill_render_and_delivery_preserve_mapping_and_prevent_duplicate_ingestion(
    monkeypatch, tmp_path, file_server
):
    config = setup(tmp_path)
    config["video"].update(width=320, height=480, fps=20, preset="ultrafast")
    config["captions"].update(font_size=30, safe_margin=20)
    monkeypatch.setenv("WHISPER_API_KEY", "test-only-transcription")
    monkeypatch.setattr(
        "app.bootstrap.Transcriber",
        lambda config: SimpleNamespace(
            transcribe=lambda *args: Transcript((Word("Hello.", 0.1, 0.7),), ())
        ),
    )
    updates = []
    table = SimpleNamespace(
        jobs=lambda: [Job(92, "duplicate-label", file_server[0] + "/file", "NOT_PROCESSED")],
        image_urls=lambda: pytest.fail("Voice import must never fetch image assets"),
        update=lambda job, status, **values: updates.append((job.row, status, values)),
    )
    uploads = []

    def upload(path, progress):
        assert not list(Path(config["paths"]["buffer"]).glob("voice-*.*"))
        uploads.append(path)
        return {
            "url": "https://res.cloudinary.com/test/reel.mp4",
            "public_id": path.stem,
            "bytes": path.stat().st_size,
        }

    service = BufferAutomations(
        config,
        table=lambda: table,
        publisher=lambda: SimpleNamespace(public_id=lambda path: path.stem, upload=upload),
    )
    assert service.refill() == 1
    voice = next(Path(config["paths"]["buffer"]).glob("voice-*.audio"))
    assert voice.name == "voice-000001.audio" and voice.read_bytes() == file_server[1]
    image_dir = Path(config["composition"]["images_directory"])
    assert not image_dir.exists(), "Voice import must work without an image pool or image table"
    image_dir.mkdir(parents=True)
    image = image_dir / "shared.png"
    Image.new("RGB", (160, 120), "blue").save(image)
    original_image = image.read_bytes()
    assert service.refill() == 0
    processor = Processor(config)
    try:
        processor.start()
        processor.thread.join(timeout=20)
        assert not processor.active
        assert processor.status()["completed"] == 1, processor.status()
    finally:
        processor.close()
    assert not voice.exists() and updates == [] and uploads == []
    assert image.read_bytes() == original_image
    assert service.refill() == 0, (
        "Unchanged remote NOT_PROCESSED must not requeue a completed voice"
    )
    assert service.deliver() == 1
    assert len(uploads) == 1
    assert updates[0][0:2] == (92, "DONE")
    assert updates[0][2]["uploaded_url"] == "https://res.cloudinary.com/test/reel.mp4"
