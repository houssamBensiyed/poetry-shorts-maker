"""Own sequential run coordination and observable processing state.

A Processor serves either the terminal dashboard or HTTP API. An in-process
guard protects lifecycle changes; a shared OS lock prevents cooperating processes
from working the same queue simultaneously. Providers and per-job policy are
injected through ProcessingServices so the coordinator only schedules work.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from app.cancellation import Cancelled, check_cancel
from app.contracts import ProcessingServices
from app.models import Job
from app.state import RunLock, State

log = logging.getLogger("video_builder")


class Processor:
    """Manage one worker thread, a stop event and the current queue snapshot.

    Runs process NOT_PROCESSED jobs sequentially from a single repository read.
    Existing PROCESSING rows are left for manual recovery. Use status() for
    display data and close() to wait for a safe worker shutdown.
    """

    def __init__(
        self,
        config: dict[str, Any],
        services: ProcessingServices | None = None,
        *,
        run_lock: RunLock | None = None,
    ) -> None:
        """Initialize local state without reading jobs or starting a worker.

        Args:
            config: Application settings copied to isolate them from caller mutation.
            services: Optional provider composition; omitted selects bootstrap defaults.
            run_lock: Optional lock implementation for tests or alternative ownership.

        Default RunLock initialization creates its lock directory, but provider
        credentials and network access are deferred until refresh or start.
        """
        self.config = deepcopy(config)
        self.default_services = services is None
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
        """Return whether this instance's worker thread is alive.

        This is local ownership information; status() also reports another process
        holding the shared queue lock.
        """
        with self.guard:
            return self.thread is not None and self.thread.is_alive()

    def update(self, **values: Any) -> None:
        """Merge state fields and publish a snapshot while this instance owns the run.

        Shared-status filesystem errors are logged without escaping so observability
        cannot change job results or prevent eventual lock release.
        """
        with self.guard:
            self.state.update(**values)
            if self.run_lock.handle:
                try:
                    self.run_lock.publish(self.state.snapshot())
                except OSError:
                    # Observability must not change a job's outcome or leak run ownership.
                    log.warning("Could not publish shared processing status", exc_info=True)

    def status(self) -> dict[str, Any]:
        """Return a detached local or externally published state dictionary.

        external_process identifies another lock holder, allowing the dashboard to
        display its work without offering to stop a thread owned by another process.
        Shared state is best-effort display information, not a recovery checkpoint.
        """
        with self.guard:
            if not self.run_lock.handle and self.run_lock.busy():
                return {**self.run_lock.external_state(), "external_process": True}
            return {**self.state.snapshot(), "external_process": False}

    def start(
        self,
        *,
        prepare: Callable[[threading.Event], None] | None = None,
        empty_message: str | None = None,
        mode: str | None = None,
        channel_name: str | None = None,
    ) -> bool:
        """Acquire queue ownership and launch a non-daemon worker thread.

        Return False if closed, refreshing, locally active, or locked by another
        process. True means the worker started, not that validation or jobs succeeded.
        On thread-start failure, publish local error state, release the lock and
        re-raise so a broken start cannot leave the queue permanently claimed.
        Optional preparation runs in the worker under the queue lock and shares
        cancellation. empty_message rejects an empty queue after preparation;
        omitted, an empty batch remains a successful no-op for polling callers.
        mode selects a private local composition for this run without changing
        defaults. Injected custom services own their mode policy and reject overrides.
        """
        with self.guard:
            if self.closed or self.refreshing or self.active:
                return False
            services = self.services
            if mode is not None or channel_name is not None:
                if mode is not None and mode not in ("image", "video", "hybrid"):
                    raise ValueError("Footage mode must be image, video or hybrid")
                if not self.default_services:
                    raise ValueError("Mode overrides require the default local composition")
                from app.bootstrap import build_services
                from app.config import normalize_channel_name

                run_config = deepcopy(self.config)
                if mode is not None:
                    run_config["footage"]["mode"] = mode
                if channel_name is not None:
                    run_config["captions"]["channel_name"] = normalize_channel_name(channel_name)
                services = build_services(run_config)
            if not self.run_lock.acquire():
                return False
            try:
                self.stop_event.clear()
                self.update(
                    status="processing",
                    stage="Reading job queue",
                    error="",
                    current_job=None,
                    voiceover="",
                    render_progress=None,
                    mode=mode or self.config["footage"]["mode"],
                    channel_name=channel_name
                    if channel_name is not None
                    else self.config["captions"]["channel_name"],
                )
                self.thread = threading.Thread(
                    target=self._run,
                    args=(prepare, empty_message, services),
                    name="video-processor",
                    daemon=False,
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
        """Request cooperative cancellation and return without waiting for the worker.

        Adapters observe the event at safe checkpoints. In-flight HTTP calls may
        continue until a response or timeout, and confirmed uploads are still committed.
        """
        self.stop_event.set()
        log.info("Stop requested; operations stop at their next safe checkpoint")

    def close(self) -> None:
        """Prevent future starts, request stop and join the local worker.

        The join occurs outside the lifecycle guard so worker cleanup can acquire it.
        Avoid joining the current thread. Shutdown can wait for an in-flight request
        to reach a cancellation checkpoint or its configured timeout.
        """
        with self.guard:
            self.closed = True
            self.stop()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join()

    def refresh(self) -> dict[str, Any]:
        """Read queue counts without starting jobs, or return status when busy.

        Serialize refresh/start requests with the lifecycle guard. Construct the
        repository without the previous run's stop event so a completed cancellation
        cannot block manual refresh. Record errors in state and re-raise for the UI
        to log; always clear the refreshing flag.
        """
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
        """Count pending, completed and failed rows in a job snapshot.

        COMPLETED is accepted as a legacy success status alongside DONE. PROCESSING
        and unknown statuses are excluded from these three counters.
        """
        return dict(
            pending=sum(j.status == "NOT_PROCESSED" for j in jobs),
            completed=sum(j.status in {"DONE", "COMPLETED"} for j in jobs),
            failed=sum(j.status == "FAILED" for j in jobs),
        )

    def _run(
        self,
        prepare: Callable[[threading.Event], None] | None = None,
        empty_message: str | None = None,
        services: ProcessingServices | None = None,
    ) -> None:
        """Validate services, snapshot the queue and execute pending jobs in order.

        Read the source catalog once only when work exists. Decrement pending as
        each job starts and increment the result counter returned by the executor.
        Per-job failures normally return False; an unexpected executor or provider
        exception stops the batch and becomes visible error state. Clear transient
        fields and release ownership in finally, including after cancellation.
        """
        services = services if services is not None else self.services
        try:
            check_cancel(self.stop_event)
            if prepare is not None:
                self.update(stage="Preparing voice buffer")
                prepare(self.stop_event)
                check_cancel(self.stop_event)
            services.validate()
            check_cancel(self.stop_event)
            repository = services.repository(self.stop_event)
            jobs = repository.jobs()
            self.update(**self._counts(jobs))
            pending = [job for job in jobs if job.status == "NOT_PROCESSED"]
            if not pending and empty_message is not None:
                raise ValueError(empty_message)
            stuck = sum(job.status == "PROCESSING" for job in jobs)
            if stuck:
                log.warning(
                    "%s rows already PROCESSING: inspect output and use manual recovery", stuck
                )
            log.info("Found %d pending jobs", len(pending))
            if pending:
                sources = services.catalog(self.stop_event).sources()
                executor = services.executor(self.stop_event, self.update)
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
