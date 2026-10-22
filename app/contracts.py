"""Small structural interfaces: adapters need no common base class."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from app.models import Job, Transcript, UploadResult
from app.timeline import Plan, Source

Progress = Callable[[float], None]


class Reporter(Protocol):
    def __call__(self, **values: Any) -> None: ...


class JobRepository(Protocol):
    def jobs(self) -> list[Job]: ...

    def update(
        self,
        job: Job,
        status: str,
        output: str = "",
        error: str = "",
        stop: Event | None = None,
        uploaded_url: str = "",
    ) -> None: ...


class SourceCatalog(Protocol):
    def sources(self) -> Sequence[Source]: ...


class FileDownloader(Protocol):
    def download(self, url: str, destination: Path) -> Path: ...


class SpeechTranscriber(Protocol):
    def transcribe(
        self,
        path: Path,
        duration: float,
        stop: Event,
        workdir: Path,
    ) -> Transcript: ...


class VideoPublisher(Protocol):
    def public_id(self, path: Path) -> str: ...

    def upload(self, path: Path, progress: Progress) -> UploadResult: ...


class Renderer(Protocol):
    def __call__(
        self,
        plan: Plan,
        audio: Path,
        output: Path,
        work: Path,
        stop: Event,
        progress: Progress,
    ) -> Path: ...


class JobExecutor(Protocol):
    def run(self, job: Job, repository: JobRepository, sources: Sequence[Source]) -> bool:
        """Return True only after the repository acknowledges completion."""
        ...


@dataclass(frozen=True)
class ProcessingServices:
    """Factories delay credentials and I/O until the user starts a run."""

    identity: str
    repository: Callable[[Event | None], JobRepository]
    catalog: Callable[[Event], SourceCatalog]
    executor: Callable[[Event, Reporter], JobExecutor]
    validate: Callable[[], None]
