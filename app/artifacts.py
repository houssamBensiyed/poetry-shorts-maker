"""Local job paths and atomic recovery checkpoints."""

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
    work: Path
    output: Path
    audio: Path
    images: Path

    @classmethod
    def for_job(cls, paths: dict[str, str], job: Job) -> "Workspace":
        name = f"row-{job.row}-{uuid.uuid4().hex}"
        return cls(
            work=Path(paths["temp"]) / name,
            output=Path(paths["output"]) / f"{name}.mp4",
            audio=Path(paths["buffer"]) / f"{name}.audio",
            images=Path(paths["image_buffer"]) / name,
        )

    @property
    def output_reference(self) -> str:
        return (
            str(self.output.relative_to(ROOT))
            if self.output.is_relative_to(ROOT)
            else str(self.output)
        )

    def prepare(self) -> None:
        self.work.mkdir(parents=True, exist_ok=False)

    def write(self, name: str, data: Any) -> None:
        """Replace complete JSON atomically; never expose a partial checkpoint."""
        destination = self.work / f"{name}.json"
        temporary = destination.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def clean(self) -> None:
        shutil.rmtree(self.work)
