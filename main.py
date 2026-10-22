"""Launch the interactive dashboard: python main.py"""

import signal

from app.config import load_config
from app.dashboard import Dashboard
from app.logger import configure
from app.processor import Processor


def main():
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
