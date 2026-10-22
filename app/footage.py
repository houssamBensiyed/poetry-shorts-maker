"""Probe local media before planning and after rendering.

FFprobe supplies stream/container facts without decoding a full video in Python.
The pipeline uses the original audio's duration as its timeline clock; the
renderer uses the same probe to check its final encoded output.
"""

import json
import math
import subprocess
from pathlib import Path


def probe(path):
    """Return FFprobe's decoded stream and format JSON for path.

    Run with a 30-second timeout and captured output. Raise ValueError with a
    bounded diagnostic tail when FFprobe fails; executable, timeout and JSON
    errors propagate. The filename in diagnostics avoids exposing the full path.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError(f"FFprobe could not read {Path(path).name}: {result.stderr[-1500:]}")
    return json.loads(result.stdout)


def audio_duration(path):
    """Return positive finite seconds from the first audio stream or container.

    Require an actual audio stream before considering format-level duration, so
    a readable image/video without audio cannot become a voiceover. Prefer the
    stream duration and fall back to the container when it is unavailable.
    Raise ValueError for missing audio or empty/unmeasurable duration.
    """
    data = probe(path)
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if not audio:
        raise ValueError("Downloaded file has no audio stream")
    duration = float(audio.get("duration") or data["format"].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio is empty or has no measurable duration")
    return duration
