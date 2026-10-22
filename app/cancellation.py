"""Share a cooperative stop signal without importing media or providers.

Callers check threading.Event at safe boundaries. Cancellation raises a distinct
exception that the lifecycle runner records as an interrupted job; it does not
forcibly kill Python threads or undo a confirmed remote upload.
"""

from threading import Event


class Cancelled(Exception):
    """Signal a user-requested stop observed at a safe processing checkpoint."""


def check_cancel(stop: Event) -> None:
    """Raise Cancelled when stop is set; otherwise return without side effects.

    The recovery-oriented message explains how to make an interrupted row
    eligible again. Actual retry decisions remain with the operator.
    """
    if stop.is_set():
        raise Cancelled("Processing cancelled; reset the row to NOT_PROCESSED to retry")
