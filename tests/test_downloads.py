from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import wave
import io
import pytest
from app.downloads import Downloader
from app.footage import audio_duration
from app.video_builder import Cancelled


def downloader(stop=None):
    return Downloader({'timeout_seconds':3,'max_file_mb':1},stop or threading.Event())


def test_downloads_original_bytes_through_redirect_into_buffer(file_server,tmp_path):
    url,audio=file_server
    path=downloader().download(url+'/redirect',tmp_path/'buffer'/'voiceover.audio')
    assert path.read_bytes()==audio
    assert audio_duration(path)==1
    assert not list(path.parent.glob('*.partial'))


@pytest.mark.parametrize('suffix,message',[('/login','web/login'),('/missing','404'),('/empty','empty'),('/large','limit')])
def test_invalid_downloads_leave_no_partial(file_server,tmp_path,suffix,message):
    with pytest.raises(ValueError,match=message): downloader().download(file_server[0]+suffix,tmp_path/'audio')
    assert not list(tmp_path.iterdir())


def test_existing_audio_not_overwritten(tmp_path):
    path=tmp_path/'original'; path.write_bytes(b'original')
    with pytest.raises(ValueError,match='overwrite'): downloader().download('https://example.com/audio',path)
    assert path.read_bytes()==b'original'


def test_cancellation_prevents_download(tmp_path):
    stop=threading.Event(); stop.set()
    with pytest.raises(Cancelled): downloader(stop).download('https://example.com/audio',tmp_path/'audio')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url',['file:///etc/passwd','ftp://example.com/audio','https://name:pass@example.com/audio'])
def test_non_http_links_rejected(tmp_path,url):
    with pytest.raises(ValueError,match='HTTP'): downloader().download(url,tmp_path/'audio')
