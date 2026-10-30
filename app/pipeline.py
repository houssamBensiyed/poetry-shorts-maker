"""Prepare a video's media independently of storage and publication policy.

The pipeline downloads original audio, probes its clock, transcribes speech,
plans the timeline, resolves selected sources, and renders. It records the
transcript but leaves status transitions, upload checkpoints and cleanup to
JobRunner. Injected dependencies make media stages independently replaceable.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event

from app.artifacts import Workspace
from app.cancellation import check_cancel
from app.contracts import FileDownloader, Renderer, Reporter, SpeechTranscriber
from app.models import Job, Transcript
from app.timeline import Plan, Source

log = logging.getLogger("video_builder")


@dataclass
class MediaPipeline:
    """Compose the six dependencies required to turn audio into a local MP4.

    Attributes:
        downloader: Buffers the original audio without relying on its extension.
        transcriber: Produces validated timestamped speech from local audio.
        duration: Probe callable returning positive audio duration in seconds.
        plan: Pure strategy mapping transcript, candidates and duration to a Plan.
        resolve: Strategy replacing selected candidates with renderable local media.
        render: Callable that finalizes and validates the output before returning.

    The default composition uses still images; a different resolver can support
    other source types without changing the queue or job lifecycle.
    """

    downloader: FileDownloader
    transcriber: SpeechTranscriber
    duration: Callable[[Path], float]
    plan: Callable[[Transcript, Sequence[Source], float], Plan]
    resolve: Callable[[Plan, Path, Event], Plan]
    render: Renderer

    def run(
        self,
        job: Job,
        workspace: Workspace,
        sources: Sequence[Source],
        stop: Event,
        report: Reporter,
    ) -> None:
        """Execute media stages and leave the finished video at workspace.output.

        Args:
            job: Snapshot containing the direct audio link.
            workspace: Prepared per-attempt paths, including the checkpoint directory.
            sources: Candidate source snapshot shared by the run.
            stop: Event checked before each stage and by cooperative adapters.
            report: Non-throwing callback for stage and render-progress state updates.

        Persist transcript.json for diagnosis. Forward exceptions to JobRunner,
        which owns failure reporting and recovery. The renderer must finish output
        atomically and return without raising after successful finalization.
        """

        def stage(name: str) -> None:
            """Check cancellation, clear prior render progress and announce a stage."""
            check_cancel(stop)
            report(stage=name, render_progress=None)
            log.info("%s", name)

        stage("Downloading")
        self.downloader.download(job.voice_link, workspace.audio)
        self.run_local(workspace, sources, stop, report)

    def run_local(
        self,
        workspace: Workspace,
        sources: Sequence[Source],
        stop: Event,
        report: Reporter,
    ) -> None:
        """Render already-buffered audio without downloading or updating providers."""

        def stage(name: str) -> None:
            check_cancel(stop)
            report(stage=name, render_progress=None)
            log.info("%s", name)

        check_cancel(stop)
        duration = self.duration(workspace.audio)
        stage("Transcribing")
        transcript = self.transcriber.transcribe(workspace.audio, duration, stop, workspace.work)
        workspace.write("transcript", asdict(transcript))
        stage("Planning footage")
        plan = self.plan(transcript, sources, duration)
        log.info(
            "Created %d footage intervals and %d captions", len(plan.footage), len(plan.captions)
        )
        stage("Resolving selected images")
        plan = self.resolve(plan, workspace.images, stop)
        stage("Rendering")
        last_bucket = -1

        def progress(value: float) -> None:
            """Publish rounded render percentages and log newly reached ten-percent buckets.

            UI updates remain frequent while disk/live logs avoid a line per FFmpeg tick.
            """
            nonlocal last_bucket
            report(render_progress=round(value, 1))
            bucket = int(value) // 10
            if bucket > last_bucket:
                log.info("Rendering: %.0f%%", value)
                last_bucket = bucket

        self.render(plan, workspace.audio, workspace.output, workspace.work, stop, progress)
