"""Apply one job's completion, publication and failure-recovery policy.

Local rendering, remote upload and repository updates cannot share a transaction.
The runner therefore checkpoints each boundary and preserves completed outputs
when later work fails. DONE means both a confirmed upload and an acknowledged
repository write; cleanup happens only after that acknowledgement.
"""

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
    """Produce local media without owning job status or publishing policy."""

    def run(
        self,
        job: Job,
        workspace: Workspace,
        sources: Sequence[Source],
        stop: Event,
        report: Reporter,
    ) -> None:
        """Create a finalized video at workspace.output or raise before completion.

        Use the supplied candidate sources, stop event and non-throwing reporter.
        The workspace has already been prepared and PROCESSING acknowledged. Do not
        raise after output finalization: the runner treats media failures as incomplete.
        """
        ...


@dataclass
class JobRunner:
    """Coordinate a single job across media, publication and persistence.

    Attributes:
        media: Workflow responsible for producing a validated local output.
        publisher: Adapter exposing the intended ID before confirmed upload.
        workspace: Factory allocating fresh local paths for each attempt.
        stop: Run-level cancellation event used by interruptible work.
        report: Non-throwing stage/error callback, normally Processor.update.

    Expected per-job exceptions return False so later jobs can still run.
    Checkpoints support manual reconciliation rather than automatic resumption.
    """

    media: MediaWorkflow
    publisher: VideoPublisher
    workspace: Callable[[Job], Workspace]
    stop: Event
    report: Reporter

    def run(self, job: Job, repository: JobRepository, sources: Sequence[Source]) -> bool:
        """Process one job and return whether DONE was acknowledged.

        Args:
            job: Repository snapshot with the stable row ID and audio link.
            repository: Adapter used for PROCESSING, DONE and FAILED writes.
            sources: Media candidate snapshot supplied to the workflow.

        Create a unique workspace, acknowledge PROCESSING, run media, and record
        rendered.json. Save the intended remote ID in upload.json before upload,
        then the confirmed result in uploaded.json before committing DONE. Completion
        and failure writes omit the stop event so cancellation cannot discard an
        already confirmed upload or prevent an independent recovery attempt.

        On exception, attempt failure.json and FAILED independently. Preserve an MP4
        after the media workflow has returned; remove incomplete output otherwise.
        Temporary cleanup errors after DONE are logged and still return True.
        """
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
                """Log upload progress only when another ten-percent bucket is reached."""
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
