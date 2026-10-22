"""Specify the behavioral boundaries used to assemble a processing run.

Protocols use structural typing: adapters implement the named methods without
inheriting a shared base class. This keeps orchestration independent of Baserow,
Cloudinary and the hosted speech API, and permits small in-memory test adapters.
Progress callbacks receive percentages from 0 to 100. Reporter callbacks receive
keyword state changes and must not raise, because reporting runs inside job work.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from app.models import Job, Transcript, UploadResult
from app.timeline import Plan, Source

Progress = Callable[[float], None]


class Reporter(Protocol):
    """Accept partial observable-state updates without controlling job outcomes."""

    def __call__(self, **values: Any) -> None:
        """Merge keyword fields such as stage, render_progress and error.

        Implementations must tolerate updates from the worker thread and avoid
        throwing: a display or shared-status failure must not invalidate media work.
        """
        ...


class JobRepository(Protocol):
    """Read job snapshots and acknowledge updates to stable job identifiers."""

    def jobs(self) -> list[Job]:
        """Return the available jobs, including statuses needed for queue counts.

        Reading must not claim jobs or start processing. The coordinator chooses
        pending entries from this snapshot and leaves recovery decisions to users.
        """
        ...

    def update(
        self,
        job: Job,
        status: str,
        output: str = "",
        error: str = "",
        stop: Event | None = None,
        uploaded_url: str = "",
    ) -> None:
        """Acknowledge status and result fields for the original job row.

        Args:
            job: Snapshot containing the stable record identifier.
            status: Requested lifecycle state, normally PROCESSING, DONE or FAILED.
            output: Local completed-video reference, or an empty string.
            error: Human-readable failure details, or an empty string.
            stop: Optional cancellation event for interruptible work. None allows
                completion and recovery writes to proceed after cancellation.
            uploaded_url: Confirmed remote video URL, or an empty string.

        Raise if persistence is unacknowledged; returning means the caller can
        proceed with the corresponding lifecycle transition.
        """
        ...


class SourceCatalog(Protocol):
    """Supply reusable media candidates separately from job persistence."""

    def sources(self) -> Sequence[Source]:
        """Return candidates compatible with the injected planner and resolver.

        Sources may still contain remote URLs. Buffering selected candidates is a
        later pipeline step, avoiding downloads of the entire catalog for every job.
        """
        ...


class FileDownloader(Protocol):
    """Buffer a remote file and expose its path only after download completion."""

    def download(self, url: str, destination: Path) -> Path:
        """Download url to destination and return the completed local Path.

        Implementations own transfer limits, partial-file cleanup and cancellation.
        An exception must not leave a partial transfer presented as completed input.
        """
        ...


class SpeechTranscriber(Protocol):
    """Convert audio into validated words and diagnostic segment timestamps."""

    def transcribe(
        self,
        path: Path,
        duration: float,
        stop: Event,
        workdir: Path,
    ) -> Transcript:
        """Transcribe local audio using the original audio's seconds as the clock.

        Args:
            path: Buffered original audio file.
            duration: Positive, finite duration obtained from media probing.
            stop: Cooperative cancellation signal shared by the processing run.
            workdir: Per-job directory for conversion files and diagnostics.

        Return a Transcript with finite, ordered word timestamps inside duration.
        Preserve word text and punctuation sufficiently for sentence-based planning;
        raise on missing or unsafe timing data rather than inventing timestamps.
        """
        ...


class VideoPublisher(Protocol):
    """Publish a completed video with an identifier available before transfer."""

    def public_id(self, path: Path) -> str:
        """Return the intended remote identifier without uploading the file.

        The runner checkpoints this value before network work so an ambiguous
        response can be reconciled against the intended remote object.
        """
        ...

    def upload(self, path: Path, progress: Progress) -> UploadResult:
        """Return UploadResult only after confirming the whole remote video.

        Report transfer percentages through progress. Raise on incomplete or
        ambiguous responses. Preserve a confirmed result when cancellation arrives
        with the final response so the runner can still commit its URL to the job.
        """
        ...


class Renderer(Protocol):
    """Finalize a complete, validated video from a resolved media plan."""

    def __call__(
        self,
        plan: Plan,
        audio: Path,
        output: Path,
        work: Path,
        stop: Event,
        progress: Progress,
    ) -> Path:
        """Render the plan, original audio and captions into output.

        Args:
            plan: Full-duration timeline whose sources are readable by the renderer.
            audio: Original buffered voiceover used in the finished video.
            output: Final destination, published atomically after validation.
            work: Per-job directory for segments, captions and diagnostic files.
            stop: Cooperative cancellation event.
            progress: Callback accepting completion percentages from 0 to 100.

        Return the finalized Path. Do not raise after successful finalization:
        the lifecycle runner interprets a media exception as incomplete output.
        """
        ...


class JobExecutor(Protocol):
    """Own one job's acknowledged completion and expected failure handling."""

    def run(self, job: Job, repository: JobRepository, sources: Sequence[Source]) -> bool:
        """Return True only after the repository acknowledges completion.

        Use the provided source snapshot for this job. Expected per-job failures
        should be reported and return False so later jobs can continue; an uncaught
        exception terminates the batch at the coordinator boundary.
        """
        ...


@dataclass(frozen=True)
class ProcessingServices:
    """Bundle lazy factories and validation for one provider composition.

    Attributes:
        identity: Stable queue namespace used by the interprocess run lock.
            Processes sharing a queue must use the same identity and lock storage.
        repository: Factory taking an optional cancellation event. None is used
            for a manual refresh that is independent of the last run's stop flag.
        catalog: Factory taking the run's stop event and returning media candidates.
        executor: Factory taking the stop event and reporter for per-job work.
        validate: Preflight check for the providers selected by this composition.

    Factories defer credential checks and network work until refresh/start, so
    the dashboard can open while credentials are still being configured.
    """

    identity: str
    repository: Callable[[Event | None], JobRepository]
    catalog: Callable[[Event], SourceCatalog]
    executor: Callable[[Event, Reporter], JobExecutor]
    validate: Callable[[], None]
