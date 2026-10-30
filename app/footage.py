"""Probe local media before planning and after rendering.

FFprobe supplies stream/container facts without decoding a full video in Python.
The pipeline uses the original audio's duration as its timeline clock; the
renderer uses the same probe to check its final encoded output.
"""

import json
import math
import subprocess
from pathlib import Path

from app.cancellation import check_cancel
from app.timeline import Source

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def validate_output(path, duration, video):
    """Shared final media contract for every rendering workflow."""
    result = probe(path)
    visual = next((s for s in result["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in result["streams"] if s["codec_type"] == "audio"), None)
    if visual is None or audio is None:
        raise ValueError("Rendered output is missing video or audio")
    numerator, denominator = map(int, visual["r_frame_rate"].split("/"))
    tolerance = 1 / video["fps"] + 0.002
    if (
        abs(float(result["format"]["duration"]) - duration) > tolerance
        or abs(float(visual["duration"]) - duration) > tolerance
        or numerator / denominator != video["fps"]
        or visual["codec_name"] != "h264"
        or audio["codec_name"] != "aac"
        or (visual["width"], visual["height"]) != (video["width"], video["height"])
    ):
        raise ValueError("Rendered output failed duration, resolution, or audio validation")


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


def video_duration(path):
    """Require a timed video stream, excluding attached cover images."""
    data = probe(path)
    video = next(
        (
            s
            for s in data["streams"]
            if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ValueError(f"{Path(path).name} has no video stream")
    duration = float(video.get("duration") or data["format"].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"{Path(path).name} has no measurable video duration")
    return duration


class LocalFootage:
    """Snapshot shared assets for the selected mode without modifying any files."""

    def __init__(self, config, stop=None):
        self.config, self.stop = config, stop

    def sources(self):
        from app.images import LocalImages

        mode = self.config["footage"]["mode"]
        if mode not in ("image", "video", "hybrid"):
            raise ValueError("Footage mode must be image, video or hybrid")
        assets = self.config["composition"]
        sources = []
        if mode in ("image", "hybrid"):
            sources.extend(LocalImages(assets["images_directory"]).sources())
        if mode in ("video", "hybrid"):
            videos = []
            for path in sorted(Path(assets["videos_directory"]).glob("*")):
                if self.stop is not None:
                    check_cancel(self.stop)
                if (
                    path.suffix.lower() in VIDEO_SUFFIXES
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    videos.append(Source(str(path.resolve()), "video", video_duration(path)))
            if not videos:
                raise ValueError(
                    "Video asset directory is empty; add videos to composition.videos_directory"
                )
            sources.extend(videos)
        return sources
