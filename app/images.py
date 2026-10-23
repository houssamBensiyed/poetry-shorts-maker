"""Read shared image assets and decode downloads for optional custom pipelines.

URLs and filename extensions cannot establish usable pixels. Pillow verifies and
fully decodes each download before its path enters the resolved timeline. The
format allowlist and decompression-bomb handling reject unsupported, animated,
corrupt or excessively large inputs before expensive video encoding.
"""

import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.cancellation import check_cancel
from app.timeline import Source

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp"}


class LocalImages:
    """Expose reusable local images; the timeline planner randomizes selection."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def sources(self):
        sources = [
            Source(str(p.resolve()), "image")
            for p in sorted(self.directory.glob("*"))
            if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file() and not p.is_symlink()
        ]
        if not sources:
            raise ValueError(
                "Image asset directory is empty; add still images to composition.images_directory"
            )
        return sources


def download_images(urls, directory, downloader, stop):
    """Return validated local image Sources for distinct URLs in first-use order.

    Args:
        urls: Selected remote image links, possibly repeated across intervals.
        directory: Per-attempt temporary directory, created as needed.
        downloader: Adapter that writes a complete file to the requested path.
        stop: Run-level cancellation event checked before each image.

    Download to neutral .download names, detect JPEG/PNG/WebP/BMP from content,
    verify and reopen to force pixel decoding, then rename with the detected
    extension. Promote decompression warnings to errors and remove raw files on
    every exit. Earlier validated images remain if a later image fails, providing
    recovery evidence. Raise ValueError for invalid image content; transfer and
    cancellation failures propagate from their respective adapters.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sources = []
    for index, url in enumerate(dict.fromkeys(urls)):
        check_cancel(stop)
        raw = directory / f"image-{index + 1:03d}.download"
        try:
            downloader.download(url, raw)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(raw) as image:
                    kind = image.format
                    if kind not in EXTENSIONS or getattr(image, "is_animated", False):
                        raise ValueError("Use a still JPEG, PNG, WebP or BMP image")
                    image.verify()
                # Force decoding: valid headers alone do not establish readable pixels.
                with Image.open(raw) as image:
                    image.load()
            target = raw.with_suffix(EXTENSIONS[kind])
            raw.replace(target)
            sources.append(Source(str(target.resolve()), "image"))
        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise ValueError(
                f"Downloaded image {index + 1} is invalid or too large to decode"
            ) from exc
        finally:
            raw.unlink(missing_ok=True)
    return sources
