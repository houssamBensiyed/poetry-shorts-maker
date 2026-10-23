"""Optional buffer refill and completion listener for Baserow/Cloudinary.

This service never renders videos. Run it explicitly alongside the independent
buffer worker. The persisted outbox is polled rather than watching raw filesystem
deletions, which cannot distinguish success from a user removing an input.
"""

import argparse
import json
import logging
import signal
import tempfile
from pathlib import Path
from threading import Event

from app.buffer import VoiceBuffer, write_json
from app.cancellation import check_cancel
from app.config import check_upload_config, load_config
from app.downloads import Downloader
from app.footage import audio_duration
from app.jobs import JobTable
from app.models import Job
from app.state import RunLock
from app.uploads import Uploader

log = logging.getLogger("video_builder")


class BufferAutomations:
    """Refill only an empty queue; deliver consumed events using stable row IDs."""

    def __init__(self, config, stop=None, *, table=None, publisher=None, downloader=None):
        self.config = config
        self.stop = stop if stop is not None else Event()
        self.buffer = VoiceBuffer(config["paths"]["buffer"])
        self.delivery = self.buffer.metadata / "delivery"
        # Factories are lazy: a nonempty buffer never requires source credentials.
        self.table = table or self._table
        self.publisher = publisher or self._publisher
        self.downloader = downloader or Downloader(config["downloads"], self.stop)

    def _table(self):
        return JobTable(self.config["baserow"], self.stop)

    def _publisher(self):
        check_upload_config(self.config)
        return Uploader(self.config["cloudinary"], self.stop)

    def _origin_key(self, origin):
        return (origin["api_url"].rstrip("/"), origin["table_id"], origin["row_id"])

    def import_for_processing(self):
        """Explicitly import an empty queue for a console processing run.

        The caller owns the processing lock. Share the automation lock with the
        polling listener so concurrent imports cannot enqueue duplicate rows.
        Publication remains a separate optional operation.
        """
        lock = RunLock(self.buffer.identity + ":automations")
        if not lock.acquire():
            raise RuntimeError("Buffer automation is busy. Wait for it to finish, then try again.")
        try:
            check_cancel(self.stop)
            if any(job.status == "NOT_PROCESSED" for job in self.buffer.jobs()):
                raise ValueError(
                    "The local buffer already contains voices. Choose Local buffer to process "
                    "them before importing from Baserow."
                )
            imported = self.refill()
            if not imported:
                raise ValueError(
                    "No new NOT_PROCESSED voices are available in Baserow. "
                    "Add ready rows to the sheet and try again."
                )
            return imported
        finally:
            lock.release()

    def refill(self):
        """Import ready source rows only when no voice is waiting locally.

        Persist the internal Baserow row ID separately from the numbered filename.
        Retained mappings prevent repeated ingestion while the remote row is still
        NOT_PROCESSED (for example while its completed reel awaits publication).
        """
        if any(job.status == "NOT_PROCESSED" for job in self.buffer.jobs()):
            return 0
        table = self.table()
        known = {
            self._origin_key(record["origin"])
            for record in self.buffer.records()
            if (record.get("origin") or {}).get("provider") == "baserow"
            and (
                any(self.buffer.directory.glob(record["voice_id"] + ".*"))
                or (self.buffer.completed / (record["voice_id"] + ".json")).exists()
            )
        }
        source = self.config["baserow"]
        jobs = [
            job
            for job in table.jobs()
            if job.status == "NOT_PROCESSED"
            and (source["api_url"].rstrip("/"), source["table_id"], job.row) not in known
        ]
        if not jobs:
            return 0
        temp = Path(self.config["paths"]["temp"])
        temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="refill-", dir=temp) as staging:
            for job in jobs:
                check_cancel(self.stop)
                audio = Path(staging) / f"source-{job.row}.audio"
                self.downloader.download(job.voice_link, audio)
                audio_duration(audio)
                self.buffer.add(
                    audio,
                    origin={
                        "provider": "baserow",
                        "api_url": source["api_url"].rstrip("/"),
                        "table_id": source["table_id"],
                        "row_id": job.row,
                        "job_id": job.id,
                    },
                )
        return len(jobs)

    def deliver(self):
        """Upload each completed reel, checkpoint its URL, then acknowledge DONE.

        Failed sheet writes retry with the saved upload, without uploading again.
        An ambiguous upload is retained as uploading and requires reconciliation;
        it is never blindly repeated. No voice is restored or video rerendered.
        """
        delivered = 0
        failures = []
        for event in self.buffer.events():
            check_cancel(self.stop)
            try:
                delivered += self._deliver_event(event)
            except Exception as exc:
                log.exception("Completion delivery failed for %s", event["voice_id"])
                failures.append(exc)
        if failures:
            raise failures[0]
        return delivered

    def _deliver_event(self, event):
        origin = event.get("origin")
        if event["state"] != "consumed" or not origin or origin.get("provider") != "baserow":
            return 0
        source = self.config["baserow"]
        if (origin["api_url"].rstrip("/"), origin["table_id"]) != (
            source["api_url"].rstrip("/"),
            source["table_id"],
        ):
            raise ValueError(f"Baserow target changed for {event['voice_id']}; event retained")
        path = self.delivery / (event["voice_id"] + ".json")
        receipt = json.loads(path.read_text()) if path.exists() else {}
        if receipt.get("state") == "done":
            return 0
        if receipt.get("state") == "uploading":
            raise RuntimeError(
                f"Reconcile Cloudinary upload for {event['voice_id']} using {path}; "
                "its response was not checkpointed"
            )
        output = Path(event["output"])
        if "uploaded" not in receipt:
            publisher = self.publisher()
            receipt = {
                "state": "uploading",
                "public_id": publisher.public_id(output),
                "output": str(output),
                "voice_id": event["voice_id"],
                "row_id": origin["row_id"],
            }
            write_json(path, receipt)
            uploaded = publisher.upload(output, lambda value: None)
            receipt = {**receipt, "state": "uploaded", "uploaded": uploaded}
            write_json(path, receipt)
        # The original row ID is independent of sequence and editable ID labels.
        job = Job(origin["row_id"], origin["job_id"], "", "NOT_PROCESSED")
        self.table().update(
            job, "DONE", output=str(output), uploaded_url=receipt["uploaded"]["url"]
        )
        write_json(path, {**receipt, "state": "done"})
        return 1

    def tick(self):
        """Serialize listeners sharing a buffer, then deliver and consider refill."""
        lock = RunLock(self.buffer.identity + ":automations")
        if not lock.acquire():
            raise RuntimeError("Another buffer automation listener is active")
        try:
            # Each operation is independent: an upload outage must not block refill.
            failures = []
            for operation in (self.deliver, self.refill):
                try:
                    operation()
                except Exception as exc:
                    log.exception("Buffer automation %s failed", operation.__name__)
                    failures.append(str(exc))
            if failures:
                raise RuntimeError("; ".join(failures))
        finally:
            lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--watch", action="store_true", help="Poll events and refill every 5 seconds"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    service = BufferAutomations(load_config(args.config))
    previous = signal.signal(signal.SIGTERM, lambda *_: service.stop.set())
    try:
        while not service.stop.is_set():
            try:
                service.tick()
            except Exception:
                if not args.watch:
                    raise
                log.exception("Automation tick failed; local rendering remains independent")
            if not args.watch:
                break
            service.stop.wait(5)
    except KeyboardInterrupt:
        service.stop.set()
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
