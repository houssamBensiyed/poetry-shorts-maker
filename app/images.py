"""Download and validate images before they become eligible footage."""
from pathlib import Path
import warnings
from PIL import Image, UnidentifiedImageError
from app.timeline import Source
from app.video_builder import check_cancel

EXTENSIONS = {'JPEG':'.jpg','PNG':'.png','WEBP':'.webp','BMP':'.bmp'}


def download_images(urls, directory, downloader, stop):
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    sources = []
    for index,url in enumerate(dict.fromkeys(urls)):
        check_cancel(stop)
        raw = directory/f'image-{index+1:03d}.download'
        try:
            downloader.download(url,raw)
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(raw) as image:
                    kind = image.format
                    if kind not in EXTENSIONS or getattr(image,'is_animated',False):
                        raise ValueError('Use a still JPEG, PNG, WebP or BMP image')
                    image.verify()
                # Force decoding: valid headers alone do not establish readable pixels.
                with Image.open(raw) as image:
                    image.load()
            target = raw.with_suffix(EXTENSIONS[kind])
            raw.replace(target)
            sources.append(Source(str(target.resolve()),'image'))
        except (UnidentifiedImageError,OSError,SyntaxError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
            raise ValueError(f'Downloaded image {index+1} is invalid or too large to decode') from exc
        finally:
            raw.unlink(missing_ok=True)
    return sources
