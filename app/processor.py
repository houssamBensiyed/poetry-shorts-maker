"""Run coordination: ownership, cancellation, queue iteration and observable state."""

import logging
import threading
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from app.cancellation import Cancelled, check_cancel
from app.contracts import ProcessingServices
from app.models import Job
from app.state import RunLock, State

log = logging.getLogger("video_builder")


class Processor:
    def __init__(
        self,
        config: dict[str, Any],
        services: ProcessingServices | None = None,
        *,
        run_lock: RunLock | None = None,
    ) -> None:
        self.config = deepcopy(config)
        if services is None:
            # Compatibility entry point; adapter construction lives in one composition module.
            from app.bootstrap import build_services

            services = build_services(self.config)
        self.services = services
        self.state = State()
        self.stop_event = threading.Event()
        self.guard = threading.RLock()
        self.thread: threading.Thread | None = None
        self.refreshing = False
        self.closed = False
        self.run_lock = run_lock if run_lock is not None else RunLock(services.identity)

    @property
    def active(self) -> bool:
        with self.guard:
            return self.thread is not None and self.thread.is_alive()

    def update(self, **values: Any) -> None:
        with self.guard:
            self.state.update(**values)
            if self.run_lock.handle:
                try:
                    self.run_lock.publish(self.state.snapshot())
                except OSError:
                    # Observability must not change a job's outcome or leak run ownership.
                    log.warning("Could not publish shared processing status", exc_info=True)

    def status(self) -> dict[str, Any]:
        with self.guard:
            if not self.run_lock.handle and self.run_lock.busy():
                return {**self.run_lock.external_state(), "external_process": True}
            return {**self.state.snapshot(), "external_process": False}

    def start(self) -> bool:
        with self.guard:
            if self.closed or self.refreshing or self.active:
                return False
            if not self.run_lock.acquire():
                return False
            try:
                self.stop_event.clear()
                self.update(
                    status="processing",
                    stage="Connecting to job repository",
                    error="",
                    current_job=None,
                    voiceover="",
                    render_progress=None,
                )
                self.thread = threading.Thread(
                    target=self._run, name="video-processor", daemon=False
                )
                self.thread.start()
            except Exception as exc:
                self.thread = None
                self.state.update(
                    status="error", stage="Could not start processing", error=str(exc)[:1000]
                )
                self.run_lock.release()
                raise
            return True

    def stop(self) -> None:
        self.stop_event.set()
        log.info("Stop requested; operations stop at their next safe checkpoint")

    def close(self) -> None:
        with self.guard:
            self.closed = True
            self.stop()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join()

    def refresh(self) -> dict[str, Any]:
        with self.guard:
            if self.closed or self.refreshing or self.active or self.run_lock.busy():
                return self.status()
            self.refreshing = True
        try:
            jobs = self.services.repository(None).jobs()
            self.state.update(**self._counts(jobs), status="idle", stage="Jobs refreshed", error="")
            return self.status()
        except Exception as exc:
            self.state.update(status="error", error=str(exc)[:1000], stage="Refresh failed")
            raise
        finally:
            with self.guard:
                self.refreshing = False

    @staticmethod
    def _counts(jobs: Sequence[Job]) -> dict[str, int]:
        return dict(
            pending=sum(j.status == "NOT_PROCESSED" for j in jobs),
            completed=sum(j.status in {"DONE", "COMPLETED"} for j in jobs),
            failed=sum(j.status == "FAILED" for j in jobs),
        )

    def _run(self) -> None:
        try:
            self.services.validate()
            check_cancel(self.stop_event)
            repository = self.services.repository(self.stop_event)
            jobs = repository.jobs()
            self.update(**self._counts(jobs))
            pending = [job for job in jobs if job.status == "NOT_PROCESSED"]
            stuck = sum(job.status == "PROCESSING" for job in jobs)
            if stuck:
                log.warning(
                    "%s rows already PROCESSING: inspect output and use manual recovery", stuck
                )
            log.info("Found %d pending jobs", len(pending))
            if pending:
                sources = self.services.catalog(self.stop_event).sources()
                executor = self.services.executor(self.stop_event, self.update)
                for job in pending:
                    check_cancel(self.stop_event)
                    self.update(
                        current_job=job.id or f"Row {job.row}",
                        voiceover=job.voice_link,
                        pending=max(0, self.state.snapshot()["pending"] - 1),
                    )
                    succeeded = executor.run(job, repository, sources)
                    counter = "completed" if succeeded else "failed"
                    self.update(**{counter: self.state.snapshot()[counter] + 1})
            self.update(
                status="idle", stage="Stopped" if self.stop_event.is_set() else "Run finished"
            )
        except Cancelled:
            self.update(status="idle", stage="Stopped")
        except Exception as exc:
            log.exception("Processing run failed")
            self.update(status="error", stage="Run failed", error=str(exc)[:1000])
        finally:
            with self.guard:
                try:
                    self.update(current_job=None, voiceover="", render_progress=None)
                finally:
                    self.run_lock.release()
