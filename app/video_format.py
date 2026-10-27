"""Resolve shared output formats without changing project defaults."""

from copy import deepcopy
from typing import Literal

AspectRatio = Literal["9:16", "16:9", "1:1", "4:5"]
VIDEO_FORMATS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def with_video_format(config, aspect_ratio=None, *, preview=False):
    """Return a private composition with even dimensions and scaled captions.

    None keeps the configured dimensions. Previews use a 360-pixel short edge;
    full-size presets use a 1080-pixel short edge. Both builders consume this
    same configuration, and active runs keep their own snapshot.
    """
    result = deepcopy(config)
    video = config["video"]
    if aspect_ratio is not None and aspect_ratio not in VIDEO_FORMATS:
        raise ValueError("Aspect ratio must be 9:16, 16:9, 1:1 or 4:5")
    width, height = VIDEO_FORMATS.get(aspect_ratio, (video["width"], video["height"]))
    if preview:
        scale = 360 / min(width, height)
        width, height = (max(2, round(n * scale / 2) * 2) for n in (width, height))
        result["video"]["preset"] = "ultrafast"
    scale = min(width, height) / min(video["width"], video["height"])
    result["video"].update(width=width, height=height)
    if scale != 1:
        for field in ("font_size", "safe_margin", "outline_width"):
            result["captions"][field] = max(1, round(config["captions"][field] * scale))
    return result
