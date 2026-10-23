"""Numbered local voice queue and persistent completion events.

Only successful rendering consumes a voice. A receipt is saved before removal,
so interruption between saving and unlinking can be recovered without rendering
again. Completion receipts outlive the voice and are independent of listeners.
Do not delete .queue: it retains sequence numbers, source identities and events.
"""

import json
import logging
import re
import shutil
import signal
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from app.artifacts import Workspace
from app.cancellation import check_cancel
from app.models import Job
from app.state import RunLock
from app.timeline import Source

log = logging.getLogger("video_builder")
VOICE_NAME = re.compile(r"voice-([0-9]{6,})\.(wav|mp3|m4a|flac|ogg|aac|opus|audio)")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def write_json(path, value):
    """Replace a JSON record atomically, keeping readers away from partial data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class VoiceBuffer:
    """Own local queue naming, ingestion, identity and acknowledged consumption."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.metadata = self.directory / ".queue"
        self.completed = self.metadata / "completed"
        self.identity = f"voice-buffer:{self.directory}"

    @contextmanager
    def mutation(self):
        lock = RunLock(self.identity + ":metadata")
        if not lock.acquire():
            raise RuntimeError("Voice buffer metadata is busy; retry the operation")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.metadata.mkdir(parents=True, exist_ok=True)
            yield
        finally:
            lock.release()

    def _voices(self):
        found = {}
        if not self.directory.exists():
            return found
        for path in sorted(self.directory.iterdir()):
            # Existing developer tools keep their sessions in subdirectories.
            if path.name.startswith(".") or (path.is_dir() and not path.is_symlink()):
                continue
            match = VOICE_NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                raise ValueError(
                    f"Invalid voice buffer entry {path.name!r}; use voice-000001.wav "
                    "or `python -m app.buffer add <audio>`"
                )
            number = int(match[1])
            if number < 1 or path.stem != f"voice-{number:06d}" or number in found:
                raise ValueError(f"Duplicate or noncanonical voice number: {path.name}")
            found[number] = path
        return found

    def records(self):
        """Read retained ingestion mappings, including already-consumed voices."""
        return [json.loads(p.read_text()) for p in sorted(self.metadata.glob("voice-*.json"))]

    def add(self, source, origin=None):
        """Copy a ready voice into the next unused number; never move the original.

        origin is opaque metadata for optional automation, never a provider
        dependency. Publish the complete file only after its mapping is saved.
        Gaps after a failed copy are allowed; sequence IDs are never reused.
        """
        source = Path(source).resolve()
        suffix = source.suffix.lower()
        if suffix.lstrip(".") not in {"wav", "mp3", "m4a", "flac", "ogg", "aac", "opus", "audio"}:
            raise ValueError("Unsupported voice extension")
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("Voice must be a nonempty local audio file")
        with self.mutation():
            numbers = list(self._voices())
            numbers += [int(r["sequence"]) for r in self.records()]
            numbers += [int(e["sequence"]) for e in self.events()]
            counter = self.metadata / "sequence.json"
            previous = json.loads(counter.read_text()) if counter.exists() else 0
            number = max([previous, *numbers], default=0) + 1
            write_json(counter, number)
            voice_id = f"voice-{number:06d}"
            destination = self.directory / (voice_id + suffix)
            temporary = self.directory / ("." + destination.name + ".tmp")
            record = self.metadata / (voice_id + ".json")
            try:
                shutil.copyfile(source, temporary)
                write_json(record, {"voice_id": voice_id, "sequence": number, "origin": origin})
                temporary.replace(destination)
            except Exception:
                record.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return destination

    def events(self):
        return [json.loads(p.read_text()) for p in sorted(self.completed.glob("voice-*.json"))]

    def _consume(self, event):
        output = Path(event["output"])
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError(f"Completed output is missing for {event['voice_id']}; voice retained")
        audio = self.directory / event["audio_name"]
        audio.unlink(missing_ok=True)
        event = {**event, "state": "consumed"}
        write_json(self.completed / (event["voice_id"] + ".json"), event)

    def jobs(self):
        """Recover interrupted removals and snapshot pending voices numerically."""
        with self.mutation():
            events = self.events()
            for event in events:
                if event["state"] == "rendered":
                    self._consume(event)
            voices = self._voices()
            done = {int(event["sequence"]) for event in events}
            if done.intersection(voices):
                raise ValueError("A consumed voice number was reused; enqueue it with a new number")
            jobs = [Job(n, p.stem, str(p), "NOT_PROCESSED") for n, p in sorted(voices.items())]
            jobs += [Job(n, f"voice-{n:06d}", "", "DONE") for n in sorted(done)]
            return jobs

    def update(self, job, status, output="", error="", stop=None, uploaded_url=""):
        """Satisfy the scheduling interface without applying remote lifecycle policy."""
        raise RuntimeError("Local voices are completed with complete(), not repository updates")

    def complete(self, job, output):
        """Save a rendered event, remove its voice, then expose the consumed event."""
        with self.mutation():
            record = self.metadata / (job.id + ".json")
            origin = json.loads(record.read_text())["origin"] if record.exists() else None
            event = {
                "voice_id": job.id,
                "sequence": job.row,
                "audio_name": Path(job.voice_link).name,
                "output": str(Path(output).resolve()),
                "origin": origin,
                "state": "rendered",
            }
            write_json(self.completed / (job.id + ".json"), event)
            self._consume(event)


class LocalImages:
    """Expose reusable local images; the timeline planner randomizes selection."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def sources(self):
        sources = [
            Source(str(p.resolve()), "image")
            for p in sorted(self.directory.glob("*"))
            if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file() and not p.is_symlink()
        ]
        if not sources:
            raise ValueError("Image buffer is empty; add still images before rendering")
        return sources


class BufferRunner:
    """Render local voices and consume them without a publisher or remote table."""

    def __init__(self, buffer, media, paths, stop, report, validate):
        self.buffer, self.media, self.paths = buffer, media, paths
        self.stop, self.report, self.validate = stop, report, validate

    def run(self, job, repository, sources):
        workspace = Workspace(
            work=Path(self.paths["temp"]) / job.id,
            output=Path(self.paths["output"]) / (job.id + ".mp4"),
            audio=Path(job.voice_link),
            images=Path(self.paths["image_buffer"]),
        )
        try:
            check_cancel(self.stop)
            self.validate()
            workspace.work.mkdir(parents=True, exist_ok=True)
            self.media.run_local(workspace, sources, self.stop, self.report)
            # Never check cancellation between successful render and recording consumption.
            self.buffer.complete(job, workspace.output)
        except Exception as exc:
            log.exception("Local voice %s failed; completed media and input retained", job.id)
            self.report(error=str(exc)[:1000])
            return False
        try:
            workspace.clean()
        except OSError:
            log.warning("Video complete; temporary files remain at %s", workspace.work)
        return True


def main():
    """Enqueue numbered voices or run the local worker without a dashboard."""
    import argparse

    from app.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["add", "run"])
    parser.add_argument("audio", type=Path, nargs="?")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--watch", action="store_true", help="Keep checking the voice buffer")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "add":
        if args.audio is None or args.watch:
            parser.error("add requires an audio file and does not accept --watch")
        print(VoiceBuffer(config["paths"]["buffer"]).add(args.audio))
        return
    if args.audio is not None:
        parser.error("run does not accept an audio file")

    from app.logger import configure
    from app.processor import Processor

    configure(config["paths"]["logs"])
    processor = Processor(config)
    shutdown = Event()

    def request_stop(*_):
        shutdown.set()
        processor.stop()

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        while not shutdown.is_set():
            if not processor.start():
                raise SystemExit("Voice buffer is already being processed")
            processor.thread.join()
            status = processor.status()
            print(json.dumps(status), flush=True)
            if not args.watch:
                if status["status"] == "error" or status["failed"]:
                    raise SystemExit(1)
                break
            shutdown.wait(5)
    except KeyboardInterrupt:
        request_stop()
    finally:
        processor.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
