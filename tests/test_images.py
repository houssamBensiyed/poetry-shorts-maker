import threading

import pytest

from app.downloads import Downloader
from app.images import download_images


def test_image_downloads_preserved_and_duplicates_downloaded_once(file_server, tmp_path):
    stop = threading.Event()
    downloader = Downloader({"timeout_seconds": 3, "max_file_mb": 1}, stop)
    url = file_server[0] + "/image"
    sources = download_images([url, url], tmp_path, downloader, stop)
    assert len(sources) == 1 and sources[0].kind == "image"
    assert sources[0].path.endswith(".png")
    assert len(list(tmp_path.iterdir())) == 1


def test_non_image_cannot_enter_footage_pool(file_server, tmp_path):
    stop = threading.Event()
    downloader = Downloader({"timeout_seconds": 3, "max_file_mb": 1}, stop)
    with pytest.raises(ValueError, match="invalid"):
        download_images([file_server[0] + "/file"], tmp_path, downloader, stop)
    assert not list(tmp_path.iterdir())
