"""Cancellable FFmpeg subprocess execution shared by media adapters."""

import logging
import queue
import subprocess
import threading
from pathlib import Path

from app.cancellation import check_cancel

log = logging.getLogger("video_builder")


def run_ffmpeg(args, cwd, stop, duration, progress, label):
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
