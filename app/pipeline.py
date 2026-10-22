"""Media preparation independent of job storage, publication and threading."""

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
        def stage(name: str) -> None:
            check_cancel(stop)
            report(stage=name, render_progress=None)
            log.info("%s", name)

        stage("Downloading")
        self.downloader.download(job.voice_link, workspace.audio)
        duration = self.duration(workspace.audio)
        stage("Transcribing")
        transcript = self.transcriber.transcribe(workspace.audio, duration, stop, workspace.work)
        workspace.write("transcript", asdict(transcript))
        stage("Planning footage")
        plan = self.plan(transcript, sources, duration)
        log.info(
            "Created %d footage intervals and %d captions", len(plan.footage), len(plan.captions)
        )
        stage("Downloading selected images")
        plan = self.resolve(plan, workspace.images, stop)
        stage("Rendering")
        last_bucket = -1

        def progress(value: float) -> None:
            nonlocal last_bucket
            report(render_progress=round(value, 1))
            bucket = int(value) // 10
            if bucket > last_bucket:
                log.info("Rendering: %.0f%%", value)
                last_bucket = bucket

        self.render(plan, workspace.audio, workspace.output, workspace.work, stop, progress)
