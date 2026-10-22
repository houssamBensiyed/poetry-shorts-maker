"""Define the values shared across provider and application boundaries.

These records keep Baserow response shapes and transcription/upload HTTP details
out of the coordinator. Frozen dataclasses prevent accidental field reassignment;
their nested dictionaries are still mutable. Timing primitives live in timeline.
"""

from dataclasses import dataclass
from typing import TypedDict

from app.timeline import Word


@dataclass(frozen=True)
class Job:
    """Describe one job from a repository snapshot.

    Attributes:
        row: Stable repository row identifier used for updates. This is distinct
            from the editable display label and must survive table reordering.
        id: User-facing label, which may be empty or duplicated.
        voice_link: Direct HTTP(S) audio URL, potentially containing access tokens.
        status: Snapshot status; the coordinator selects NOT_PROCESSED jobs only.
    """

    row: int
    id: str
    voice_link: str
    status: str


@dataclass(frozen=True)
class Transcript:
    """Carry timestamped speech independently of the transcription provider.

    Attributes:
        words: Ordered Word values with seconds measured from the audio start.
            The planner uses their restored punctuation to identify sentences.
        segments: Provider segment text/start/end dictionaries retained in the
            transcript checkpoint for inspection, rather than used as cut points.

    An empty word sequence represents no recognized speech. Adapters validate
    timestamps before constructing the transcript; this container does no checks.
    """

    words: tuple[Word, ...]
    segments: tuple[dict, ...]


class UploadResult(TypedDict):
    """Describe a publisher's confirmed, complete remote video.

    Attributes:
        url: Public HTTPS video URL to save in the job repository.
        public_id: Remote object identifier recorded for recovery and reconciliation.
        bytes: Confirmed uploaded byte count, checked against the local file by
            the default publisher before it returns.

    This TypedDict describes the result shape; it performs no runtime validation.
    """

    url: str
    public_id: str
    bytes: int
