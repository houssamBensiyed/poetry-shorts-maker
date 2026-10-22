"""Launch the terminal dashboard without automatically running queued jobs.

Configuration and logging are prepared before constructing the shared Processor.
The dashboard provides explicit start/refresh controls; provider readiness is
checked when work is requested. Run this entry point with python main.py.
"""

import signal

from app.config import load_config
from app.dashboard import Dashboard
from app.logger import configure
from app.processor import Processor


def main():
    """Run the dashboard and guarantee cooperative processor shutdown on exit.

    Report configuration read/validation errors as a concise SystemExit. Route
    SIGTERM through the dashboard's graceful-stop path so container shutdown can
    request cancellation, then close/join the worker and restore the previous
    signal handler when the terminal app exits.
    """
    try:
        config = load_config()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    logs = configure(config["paths"]["logs"])
    processor = Processor(config)
    dashboard = Dashboard(processor, logs)
    previous = signal.signal(signal.SIGTERM, lambda *_: dashboard.confirm_exit(True))
    try:
        dashboard.run()
    finally:
        processor.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
