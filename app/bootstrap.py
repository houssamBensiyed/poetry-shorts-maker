"""Composition root: select concrete providers here, not in application services."""

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
    def __init__(self, table: JobTable) -> None:
        self.table = table

    def sources(self) -> Sequence[Source]:
        return [Source(url, "image") for url in self.table.image_urls()]


def build_media_pipeline(config: dict[str, Any], stop: Event) -> MediaPipeline:
    """Default media adapters; callers can replace any pipeline dependency."""
    config = deepcopy(config)
    downloader = Downloader(config["downloads"], stop)

    def plan(transcript: Transcript, sources: Sequence[Source], duration: float) -> Plan:
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
        return render(plan, audio, output, work, config, stop, progress)

    return MediaPipeline(
        downloader, Transcriber(config["whisper"]), audio_duration, plan, resolve, render_video
    )


def build_services(config: dict[str, Any]) -> ProcessingServices:
    """Snapshot configuration so a run cannot observe edits to the caller's dict."""
    config = deepcopy(config)

    def repository(stop: Event | None) -> JobTable:
        return JobTable(config["baserow"], stop)

    def executor(stop: Event, report: Reporter) -> JobRunner:
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
