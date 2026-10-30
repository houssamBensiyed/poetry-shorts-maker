"""Compose the local buffer worker; remote automation is opt-in and separate."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

from app.buffer import BufferRunner, VoiceBuffer
from app.config import check_render_config, check_transcription_config
from app.contracts import ProcessingServices, Progress, Reporter
from app.downloads import Downloader
from app.footage import LocalFootage, audio_duration
from app.images import download_images
from app.models import Transcript
from app.pipeline import MediaPipeline
from app.timeline import Plan, Source, build_plan, sentences_from_words
from app.transcription import Transcriber
from app.video_builder import render


def build_media_pipeline(
    config: dict[str, Any], stop: Event, *, local: bool = False
) -> MediaPipeline:
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
            mode=footage["mode"] if local else None,
        )

    def resolve(plan: Plan, directory: Path, stop: Event) -> Plan:
        """Replace selected remote image URLs with validated buffered Sources.

        Deduplicate URLs in first-use order, download each once, and preserve all
        interval boundaries and repeated selections when replacing source values.
        """
        if local:
            return plan
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
    """Use numbered local voices and the configured shared footage pools.

    Baserow and Cloudinary are neither constructed nor validated here. Empty
    queues are successful no-ops, including when transcription is unconfigured.
    """
    config = deepcopy(config)
    buffer = VoiceBuffer(config["paths"]["buffer"])

    def validate_voice() -> None:
        check_render_config(config)
        check_transcription_config(config)

    def executor(stop: Event, report: Reporter) -> BufferRunner:
        return BufferRunner(
            buffer,
            build_media_pipeline(config, stop, local=True),
            config["paths"],
            stop,
            report,
            validate_voice,
        )

    return ProcessingServices(
        identity=buffer.identity,
        repository=lambda stop: buffer,
        catalog=lambda stop: LocalFootage(config, stop),
        executor=executor,
        validate=lambda: None,
    )
