"""Cooperative cancellation shared by application services and adapters."""

from threading import Event


class Cancelled(Exception):
    """The user requested cancellation at a safe checkpoint."""


def check_cancel(stop: Event) -> None:
    if stop.is_set():
        raise Cancelled("Processing cancelled; reset the row to NOT_PROCESSED to retry")
