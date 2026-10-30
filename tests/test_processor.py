import uuid
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from app.artifacts import Workspace
from app.cancellation import Cancelled
from app.config import load_config
from app.contracts import ProcessingServices
from app.execution import JobRunner
from app.models import Job, Transcript
from app.pipeline import MediaPipeline
from app.processor import Processor
from app.timeline import Source, Word, build_plan, sentences_from_words


def setup(monkeypatch, tmp_path):
    config = load_config()
    config["composition"]["images_directory"] = str(tmp_path / "assets" / "images")
    config["baserow"]["table_id"] = uuid.uuid4().int
    config["paths"].update(
        temp=str(tmp_path / "temp"),
        output=str(tmp_path / "output"),
        buffer=str(tmp_path / "buffer"),
    )
    updates = []

    class FakeTable:
        def jobs(self):
            return [
                Job(2, "duplicate", "broken", "NOT_PROCESSED"),
                Job(3, "duplicate", "valid", "NOT_PROCESSED"),
                Job(4, "done", "valid", "COMPLETED"),
            ]

        def update(self, job, status, **kwargs):
            updates.append((job.row, status, kwargs))

    class FakeDownloader:
        def download(self, link, path):
            assert link in {"broken", "valid"}
            if link == "broken":
                raise ValueError("Bad voiceover")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return path

    def render(plan, audio, output, work, stop, progress):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"output")
        progress(100)
        return output

    class FakeUploader:
        def public_id(self, path):
            return path.stem

        def upload(self, path, progress):
            return {
                "url": "https://res.cloudinary.com/example/video/upload/video.mp4",
                "public_id": path.stem,
                "bytes": path.stat().st_size,
            }

    table, publisher = FakeTable(), FakeUploader()
    media = MediaPipeline(
        downloader=FakeDownloader(),
        transcriber=SimpleNamespace(
            transcribe=lambda *args: Transcript((Word("Hello.", 0, 1),), ())
        ),
        duration=lambda path: 2,
        plan=lambda transcript, sources, duration: build_plan(
            sentences_from_words(transcript.words, duration), sources, duration
        ),
        resolve=lambda plan, directory, stop: plan,
        render=render,
    )
    services = ProcessingServices(
        identity=str(uuid.uuid4()),
        repository=lambda stop: table,
        catalog=lambda stop: SimpleNamespace(sources=lambda: [Source("unused", "image")]),
        executor=lambda stop, report: JobRunner(
            media, publisher, partial(Workspace.for_job, config["paths"]), stop, report
        ),
        validate=lambda: None,
    )
    return Processor(config, services), updates


def test_failure_does_not_stop_next_job(monkeypatch, tmp_path):
    p, updates = setup(monkeypatch, tmp_path)
    assert p.start()
    p.thread.join(timeout=5)
    assert not p.thread.is_alive()
    assert [(row, status) for row, status, _ in updates] == [
        (2, "PROCESSING"),
        (2, "FAILED"),
        (3, "PROCESSING"),
        (3, "DONE"),
    ]
    status = p.status()
    assert status["failed"] == 1 and status["completed"] == 2 and status["pending"] == 0
    assert status["status"] == "idle"
    assert len(list((tmp_path / "temp").iterdir())) == 1, "Only failed job diagnostics remain"
    assert len(list((tmp_path / "buffer").glob("*.audio"))) == 1, (
        "Successful audio remains in the local buffer"
    )


def test_cancel_is_recoverable_and_stops_queue(monkeypatch, tmp_path):
    p, updates = setup(monkeypatch, tmp_path)

    def cancel(*args):
        p.stop_event.set()
        raise Cancelled("Interrupted; retry manually")

    runner = p.services.executor(p.stop_event, p.update)
    monkeypatch.setattr(runner.media.downloader, "download", cancel)
    assert p.start()
    p.thread.join(timeout=5)
    assert [(row, status) for row, status, _ in updates] == [(2, "PROCESSING"), (2, "FAILED")]
    assert p.status()["stage"] == "Stopped"
    assert not p.run_lock.busy()


def test_complete_output_preserved_when_table_commit_fails(monkeypatch, tmp_path):
    p, updates = setup(monkeypatch, tmp_path)
    table = p.services.repository(None)
    original = table.update

    def update(job, status, **kwargs):
        if status == "DONE":
            raise ValueError("Sheet unavailable")
        return original(job, status, **kwargs)

    monkeypatch.setattr(table, "update", update)
    p.start()
    p.thread.join(timeout=5)
    assert updates[-1][1] == "FAILED"
    assert Path(updates[-1][2]["output"]).is_file()
    assert list((tmp_path / "output").glob("*.mp4"))
    assert list((tmp_path / "temp").glob("*/uploaded.json"))


def test_upload_failure_never_marks_done_and_retains_local_video(monkeypatch, tmp_path):
    p, updates = setup(monkeypatch, tmp_path)

    def fail(*args):
        raise RuntimeError("Upload unavailable")

    monkeypatch.setattr(p.services.executor(p.stop_event, p.update).publisher, "upload", fail)
    p.start()
    p.thread.join(timeout=5)
    assert not any(status == "DONE" for _, status, _ in updates)
    assert updates[-1][1] == "FAILED" and updates[-1][2]["uploaded_url"] == ""
    assert Path(updates[-1][2]["output"]).is_file()
    assert list((tmp_path / "temp").glob("*/upload.json"))


def test_confirmed_upload_survives_stop_before_status_commit(monkeypatch, tmp_path):
    p, updates = setup(monkeypatch, tmp_path)

    def complete(path, progress):
        p.stop_event.set()
        return {"url": "https://res.cloudinary.com/demo/video.mp4", "public_id": "test", "bytes": 6}

    monkeypatch.setattr(p.services.executor(p.stop_event, p.update).publisher, "upload", complete)
    p.start()
    p.thread.join(timeout=5)
    assert updates[-1][1] == "DONE"
    assert updates[-1][2]["uploaded_url"] == "https://res.cloudinary.com/demo/video.mp4"


def finish(processor):
    assert processor.start()
    processor.thread.join(timeout=5)
    assert not processor.active
    assert not processor.run_lock.busy()


def test_diagnostic_write_failure_does_not_skip_failed_update_or_next_job(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)
    original = Workspace.write

    def write(self, name, data):
        if name == "failure":
            raise OSError("Disk full")
        original(self, name, data)

    monkeypatch.setattr(Workspace, "write", write)
    finish(processor)
    assert [(row, status) for row, status, _ in updates] == [
        (2, "PROCESSING"),
        (2, "FAILED"),
        (3, "PROCESSING"),
        (3, "DONE"),
    ]


def test_workspace_creation_failure_is_isolated_to_one_job(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)
    original = Workspace.prepare

    def prepare(self):
        if self.work.name.startswith("row-2-"):
            raise OSError("Cannot create workspace")
        original(self)

    monkeypatch.setattr(Workspace, "prepare", prepare)
    finish(processor)
    assert [(row, status) for row, status, _ in updates] == [(3, "PROCESSING"), (3, "DONE")]
    assert processor.status()["failed"] == 1


def test_render_checkpoint_failure_preserves_complete_output(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)
    original = Workspace.write

    def write(self, name, data):
        if name == "rendered":
            raise OSError("Cannot persist checkpoint")
        original(self, name, data)

    monkeypatch.setattr(Workspace, "write", write)
    finish(processor)
    assert updates[-1][1] == "FAILED"
    assert Path(updates[-1][2]["output"]).read_bytes() == b"output"
    assert updates[-1][2]["uploaded_url"] == ""


def test_upload_checkpoint_failure_preserves_known_url(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)
    original = Workspace.write

    def write(self, name, data):
        if name == "uploaded":
            raise OSError("Cannot persist uploaded URL")
        original(self, name, data)

    monkeypatch.setattr(Workspace, "write", write)
    finish(processor)
    assert updates[-1][1] == "FAILED"
    assert updates[-1][2]["uploaded_url"].startswith("https://")
    assert Path(updates[-1][2]["output"]).is_file()


def test_cleanup_failure_cannot_reverse_acknowledged_done(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)

    def clean(self):
        raise OSError("Cannot remove work directory")

    monkeypatch.setattr(Workspace, "clean", clean)
    finish(processor)
    assert updates[-1][1] == "DONE"
    assert processor.status()["completed"] == 2
    assert list((tmp_path / "temp").glob("*/uploaded.json"))


def test_status_publish_failure_does_not_change_job_outcomes(monkeypatch, tmp_path):
    processor, updates = setup(monkeypatch, tmp_path)

    def publish(data):
        raise OSError("Status directory is read-only")

    monkeypatch.setattr(processor.run_lock, "publish", publish)
    finish(processor)
    assert updates[-1][1] == "DONE"
    assert processor.status()["status"] == "idle"


def test_thread_start_failure_releases_ownership(monkeypatch, tmp_path):
    import threading

    import pytest

    processor, _ = setup(monkeypatch, tmp_path)

    def start(self):
        raise RuntimeError("Cannot create thread")

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="Cannot create thread"):
        processor.start()
    assert not processor.run_lock.busy()
    assert processor.status()["status"] == "error"
    processor.close()


def test_concurrent_starts_have_one_owner(monkeypatch, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    processor, updates = setup(monkeypatch, tmp_path)
    runner = processor.services.executor(processor.stop_event, processor.update)
    original = runner.media.downloader.download
    entered, release = threading.Event(), threading.Event()

    def download(link, path):
        entered.set()
        assert release.wait(5)
        return original(link, path)

    monkeypatch.setattr(runner.media.downloader, "download", download)
    try:
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(lambda _: processor.start(), range(8)))
        assert entered.wait(5)
        assert results.count(True) == 1
    finally:
        release.set()
        processor.close()
    assert updates[-1][1] == "FAILED"  # Closing cancels before publication.
    assert not processor.run_lock.busy()
    assert not processor.start()


def test_no_pending_jobs_do_not_construct_media_adapters(monkeypatch, tmp_path):
    processor, _ = setup(monkeypatch, tmp_path)
    table = processor.services.repository(None)
    monkeypatch.setattr(table, "jobs", lambda: [Job(1, "done", "unused", "DONE")])
    from dataclasses import replace

    def unexpected(*args):
        raise AssertionError("Unused adapter was constructed")

    processor.services = replace(processor.services, catalog=unexpected, executor=unexpected)
    finish(processor)
    assert processor.status()["status"] == "idle"
    assert processor.status()["completed"] == 1


def test_configuration_is_snapshotted(tmp_path):
    config = load_config()
    config["composition"]["images_directory"] = str(tmp_path / "assets" / "images")
    processor = Processor(config)
    config["paths"]["output"] = str(tmp_path)
    assert processor.config["paths"]["output"] != str(tmp_path)
    processor.close()
    assert not processor.start()
