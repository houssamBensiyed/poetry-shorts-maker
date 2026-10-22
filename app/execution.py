"""One job's lifecycle, publication and recovery policy."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event
from typing import Protocol

from app.artifacts import Workspace
from app.cancellation import Cancelled, check_cancel
from app.contracts import JobRepository, Reporter, VideoPublisher
from app.models import Job
from app.timeline import Source

log = logging.getLogger("video_builder")


class MediaWorkflow(Protocol):
    def run(
        self,
        job: Job,
        workspace: Workspace,
        sources: Sequence[Source],
        stop: Event,
        report: Reporter,
    ) -> None: ...


@dataclass
class JobRunner:
    media: MediaWorkflow
    publisher: VideoPublisher
    workspace: Callable[[Job], Workspace]
    stop: Event
    report: Reporter

    def run(self, job: Job, repository: JobRepository, sources: Sequence[Source]) -> bool:
        workspace = None
        completed_output = uploaded_url = ""
        marked = False
        try:
            check_cancel(self.stop)
            workspace = self.workspace(job)
            workspace.prepare()
            repository.update(job, "PROCESSING", stop=self.stop)
            marked = True
            self.media.run(job, workspace, sources, self.stop, self.report)
            # Record completion before any fallible metadata write. Never delete a complete MP4.
            completed_output = workspace.output_reference
            workspace.write(
                "rendered",
                {
                    "output": completed_output,
                    "row": job.row,
                    "audio": str(workspace.audio),
                },
            )
            check_cancel(self.stop)
            self.report(stage="Uploading video", render_progress=None)
            workspace.write(
                "upload",
                {
                    "public_id": self.publisher.public_id(workspace.output),
                    "output": completed_output,
                    "row": job.row,
                },
            )
            last_bucket = -1

            def progress(value: float) -> None:
                nonlocal last_bucket
                bucket = int(value) // 10
                if bucket > last_bucket:
                    log.info("Upload: %.0f%%", value)
                    last_bucket = bucket

            uploaded = self.publisher.upload(workspace.output, progress)
            uploaded_url = uploaded["url"]
            workspace.write("uploaded", {**uploaded, "output": completed_output, "row": job.row})
            # A confirmed upload must be committed even if Stop arrived with its response.
            self.report(stage="Updating job status", render_progress=None)
            repository.update(job, "DONE", output=completed_output, uploaded_url=uploaded_url)
        except Exception as exc:
            error = str(exc)[:1000]
            if isinstance(exc, Cancelled):
                log.warning("Job row %s interrupted; reset to NOT_PROCESSED to recover", job.row)
            else:
                log.exception("Job row %s failed", job.row)
            if workspace is not None:
                # Diagnostics and cleanup must never prevent the remote failure update.
                try:
                    workspace.write(
                        "failure",
                        {
                            "row": job.row,
                            "error": error,
                            "output": completed_output,
                            "uploaded_url": uploaded_url,
                            "audio": str(workspace.audio),
                        },
                    )
                except Exception:
                    log.exception("Could not write failure diagnostics for row %s", job.row)
                if not completed_output:
                    try:
                        workspace.output.unlink(missing_ok=True)
                    except OSError:
                        log.exception("Could not remove incomplete output for row %s", job.row)
            if marked:
                try:
                    repository.update(
                        job,
                        "FAILED",
                        output=completed_output,
                        uploaded_url=uploaded_url,
                        error=error,
                    )
                except Exception:
                    log.exception(
                        "Could not update failed row %s; manual recovery required", job.row
                    )
            self.report(error=error)
            return False

        # Cleanup is outside the transaction: an acknowledged DONE must not become FAILED.
        log.info("Job row %s completed: %s", job.row, completed_output)
        try:
            workspace.clean()
        except OSError:
            log.warning("Completed output is safe; temporary files remain at %s", workspace.work)
        return True
