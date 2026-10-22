"""Select and assemble the default providers at the composition boundary.

Application services consume protocols and injected callables; this module knows
that the defaults use Baserow, direct downloads, hosted transcription, FFmpeg and
Cloudinary. Replace dependencies here or supply ProcessingServices to Processor
when adding another provider, rather than adding provider logic to the queue.
"""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any

from app.artifacts import Workspace
from app.config import check_job_config
from app.contracts import ProcessingServices, Progress, Reporter
from app.downloads import Downloader
from app.execution import JobRunner
from app.footage import audio_duration
from app.images import download_images
from app.jobs import JobTable
from app.models import Transcript
from app.pipeline import MediaPipeline
from app.timeline import Plan, Source, build_plan, sentences_from_words
from app.transcription import Transcriber
from app.uploads import Uploader
from app.video_builder import render


class BaserowImageCatalog:
    """Adapt Baserow image URLs to the independent SourceCatalog interface."""

    def __init__(self, table: JobTable) -> None:
        """Retain the configured table adapter; defer catalog I/O until sources()."""
        self.table = table

    def sources(self) -> Sequence[Source]:
        """Read usable image links as unresolved image Sources.

        Downloads happen after random selection, keeping the catalog read lightweight
        even when the image table is much larger than a job's chosen image set.
        """
        return [Source(url, "image") for url in self.table.image_urls()]


def build_media_pipeline(config: dict[str, Any], stop: Event) -> MediaPipeline:
    """Build the default media dependencies around a private config snapshot.

    Args:
        config: Full validated application configuration.
        stop: Event shared with downloads, transcription and rendering.

    Return a MediaPipeline whose planner groups speech into sentences, resolver
    buffers selected images, and renderer uses configured FFmpeg settings.
    Copying config prevents caller edits from changing an assembled pipeline.
    """
    config = deepcopy(config)
    downloader = Downloader(config["downloads"], stop)

    def plan(transcript: Transcript, sources: Sequence[Source], duration: float) -> Plan:
        """Group timestamped words and select footage using configured cut rules.

        Caption timing follows speech; image changes occur only at sentence starts
        that meet the word threshold. The configured seed permits reproducible plans.
        """
        footage = config["footage"]
        sentences = sentences_from_words(transcript.words, duration)
        return build_plan(
            sentences,
            sources,
            duration,
            footage["minimum_words_for_new_clip"],
            footage["random_seed"],
            footage["avoid_immediate_repetition"],
        )

    def resolve(plan: Plan, directory: Path, stop: Event) -> Plan:
        """Replace selected remote image URLs with validated buffered Sources.

        Deduplicate URLs in first-use order, download each once, and preserve all
        interval boundaries and repeated selections when replacing source values.
        """
        urls = list(dict.fromkeys(interval.source.path for interval in plan.footage))
        selected = download_images(urls, directory, downloader, stop)
        mapping = dict(zip(urls, selected, strict=True))
        return replace(
            plan,
            footage=tuple(
                replace(interval, source=mapping[interval.source.path]) for interval in plan.footage
            ),
        )

    def render_video(
        plan: Plan,
        audio: Path,
        output: Path,
        work: Path,
        stop: Event,
        progress: Progress,
    ) -> Path:
        """Bind video and caption configuration to the renderer's narrow contract."""
        return render(plan, audio, output, work, config, stop, progress)

    return MediaPipeline(
        downloader, Transcriber(config["whisper"]), audio_duration, plan, resolve, render_video
    )


def build_services(config: dict[str, Any]) -> ProcessingServices:
    """Return lazy default factories using a deep copy of configuration.

    The queue identity combines the Baserow API URL and audio table ID so the
    dashboard and API contend for the same local run lock. Factory construction
    performs no provider requests; validation is deferred until a run starts.
    """
    config = deepcopy(config)

    def repository(stop: Event | None) -> JobTable:
        """Create a Baserow adapter with optional cancellation for this operation."""
        return JobTable(config["baserow"], stop)

    def executor(stop: Event, report: Reporter) -> JobRunner:
        """Assemble media, publisher and workspace factory for a processing run.

        All jobs in the run share the stop event and reporter, while each attempt
        obtains unique artifact paths from the workspace factory.
        """
        media = build_media_pipeline(config, stop)
        return JobRunner(
            media,
            Uploader(config["cloudinary"], stop),
            partial(Workspace.for_job, config["paths"]),
            stop,
            report,
        )

    return ProcessingServices(
        identity=config["baserow"]["api_url"].rstrip("/")
        + "/"
        + str(config["baserow"]["table_id"]),
        repository=repository,
        catalog=lambda stop: BaserowImageCatalog(repository(stop)),
        executor=executor,
        validate=partial(check_job_config, config),
    )
