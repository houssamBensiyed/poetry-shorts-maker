"""Provider-independent values exchanged by the processing workflow."""

from dataclasses import dataclass
from typing import TypedDict

from app.timeline import Word


@dataclass(frozen=True)
class Job:
    row: int
    id: str
    voice_link: str
    status: str


@dataclass(frozen=True)
class Transcript:
    words: tuple[Word, ...]
    segments: tuple[dict, ...]


class UploadResult(TypedDict):
    url: str
    public_id: str
    bytes: int
