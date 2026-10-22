"""Allocate per-attempt paths and persist local recovery checkpoints.

Work directories contain disposable render intermediates and JSON evidence.
Original audio, selected images and completed MP4s use separate locations so
successful cleanup cannot remove them. JSON replacement is atomic for readers;
it does not provide power-loss durability or a transaction with remote services.
"""

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.models import Job


@dataclass(frozen=True)
class Workspace:
    """Group paths belonging to one job attempt without performing I/O.

    Attributes:
        work: Unique temporary directory for checkpoints and render intermediates.
        output: Final MP4 path retained after acknowledged completion.
        audio: Buffered original audio path retained for inspection and reuse.
        images: Directory for this attempt's downloaded, selected still images.
    """

    work: Path
    output: Path
    audio: Path
    images: Path

    @classmethod
    def for_job(cls, paths: dict[str, str], job: Job) -> "Workspace":
        """Choose collision-resistant paths using the stable row ID and a UUID.

        Args:
            paths: Configured temp, output, buffer and image_buffer directories.
            job: Repository record whose row identifies the originating job.

        Return a Workspace without creating directories. A fresh UUID separates
        retries and prevents duplicate display labels from overwriting old artifacts.
        """
        name = f"row-{job.row}-{uuid.uuid4().hex}"
        return cls(
            work=Path(paths["temp"]) / name,
            output=Path(paths["output"]) / f"{name}.mp4",
            audio=Path(paths["buffer"]) / f"{name}.audio",
            images=Path(paths["image_buffer"]) / name,
        )

    @property
    def output_reference(self) -> str:
        """Return a project-relative output reference when possible.

        Paths outside the project remain absolute. This keeps normal repository
        values concise while preserving custom output locations unambiguously.
        """
        return (
            str(self.output.relative_to(ROOT))
            if self.output.is_relative_to(ROOT)
            else str(self.output)
        )

    def prepare(self) -> None:
        """Create the work directory, failing if it already exists.

        Exclusive creation catches accidental attempt reuse. Buffer and output
        directories are created later by the adapters that write their files.
        """
        self.work.mkdir(parents=True, exist_ok=False)

    def write(self, name: str, data: Any) -> None:
        """Replace a named JSON checkpoint atomically within the work directory.

        Args:
            name: Internal checkpoint stem, such as rendered, upload or failure.
            data: JSON-serializable recovery information.

        Write UTF-8 to a sibling temporary file, then rename it over the checkpoint
        so readers observe a complete document. Remove the temporary file on exit.
        Serialization and filesystem errors propagate to the lifecycle policy.
        """
        destination = self.work / f"{name}.json"
        temporary = destination.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def clean(self) -> None:
        """Remove temporary work after DONE has been acknowledged.

        Audio, images and final output live elsewhere and remain available. OSError
        propagates for the runner to log without reversing successful completion.
        """
        shutil.rmtree(self.work)
