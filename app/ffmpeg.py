"""Run FFmpeg with responsive cancellation, progress and retained diagnostics.

A reader thread drains FFmpeg's machine-readable progress stream into a queue.
The controlling thread polls that queue so a silent encoder cannot prevent stop
checks. Stderr goes to a per-stage file, keeping diagnostics out of the progress
parser and available in a failed job's retained workspace.
"""

import logging
import queue
import subprocess
import threading
from pathlib import Path

from app.cancellation import check_cancel

log = logging.getLogger("video_builder")


def run_ffmpeg(args, cwd, stop, duration, progress, label):
    """Execute one FFmpeg stage and return only after successful process exit.

    Args:
        args: FFmpeg arguments appended after standard noninteractive/progress flags.
        cwd: Existing working directory holding outputs and the stage log.
        stop: Cooperative cancellation event polled while progress is consumed.
        duration: Positive expected stage duration in seconds for progress scaling.
        progress: Non-throwing callback receiving clamped percentages from 0 to 100.
        label: Human-readable stage name, also used to name its diagnostic log.

    Convert out_time_us progress into percentages. On nonzero exit, raise
    RuntimeError with the last 3,000 diagnostic characters. On cancellation or
    callback error, terminate the subprocess, wait up to three seconds, then kill
    if necessary. Close stdout and join the reader before leaving; retain the log.
    """
    check_cancel(stop)
    log.info("%s", label)
    log_path = Path(cwd) / (label.replace(" ", "_") + ".log")
    with log_path.open("w") as error_log:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"] + args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=error_log,
            text=True,
        )
        lines = queue.Queue()

        def read():
            """Drain stdout to the queue and always send an end-of-stream sentinel.

            Running this blocking read separately lets the controlling thread continue
            cancellation checks even when FFmpeg has not emitted another progress line.
            """
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while True:
                check_cancel(stop)
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith("out_time_us="):
                    try:
                        value = float(line.split("=", 1)[1]) / 1e6 / duration * 100
                    except ValueError:
                        continue
                    progress(min(100, max(0, value)))
            code = proc.wait()
            if code:
                tail = log_path.read_text()[-3000:]
                raise RuntimeError(f"{label} failed (exit {code}). {tail}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            reader.join(timeout=2)
            proc.stdout.close()
    check_cancel(stop)
