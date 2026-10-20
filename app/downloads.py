"""Download a file from a direct HTTP(S) link; no storage-provider SDK."""
import logging
from pathlib import Path
from urllib.parse import urlparse
import httpx
from app.video_builder import check_cancel

log = logging.getLogger('video_builder')


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP/HTTPS file URL without embedded username/password')


class Downloader:
    def __init__(self, config, stop):
        self.config = config
        self.stop = stop

    def download(self, url, destination):
        validate_url(url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            raise ValueError('Download destination already exists; refusing to overwrite the buffered file')
        partial = destination.with_name(destination.name+'.partial')
        maximum = self.config['max_file_mb']*1024*1024
        try:
            with httpx.Client(timeout=self.config['timeout_seconds'],follow_redirects=True,max_redirects=5) as client:
                for attempt in range(4):
                    check_cancel(self.stop)
                    try:
                        with client.stream('GET',url) as response:
                            if response.status_code in {408,429,500,502,503,504}:
                                failure = f'HTTP {response.status_code}'
                            elif response.status_code != 200:
                                raise ValueError(f'File download returned HTTP {response.status_code}; check that the link grants download access')
                            else:
                                content_type = response.headers.get('content-type','').split(';')[0].lower()
                                if content_type in {'text/html','application/xhtml+xml'}:
                                    raise ValueError('The link returned a web/login page. Use a direct download link to the file.')
                                size = response.headers.get('content-length')
                                if size and size.isdigit() and int(size)>maximum:
                                    raise ValueError(f'File exceeds the {self.config["max_file_mb"]} MB download limit')
                                count = 0
                                with partial.open('wb') as target:
                                    for chunk in response.iter_bytes(chunk_size=64*1024):
                                        check_cancel(self.stop)
                                        count += len(chunk)
                                        if count>maximum:
                                            raise ValueError(f'File exceeds the {self.config["max_file_mb"]} MB download limit')
                                        target.write(chunk)
                                if count==0:
                                    raise ValueError('Downloaded file is empty')
                                check_cancel(self.stop)
                                partial.replace(destination)
                                log.info('Downloaded %s bytes to local buffer: %s',count,destination.name)
                                return destination
                    except (httpx.TimeoutException,httpx.TransportError) as exc:
                        # URLs may contain signed access tokens. Do not include them in logs/errors.
                        failure = type(exc).__name__
                    partial.unlink(missing_ok=True)
                    if attempt==3:
                        raise RuntimeError(f'File download failed after 4 attempts ({failure})')
                    log.warning('Temporary download failure (%s); retrying',failure)
                    self.stop.wait(2**attempt)
        finally:
            partial.unlink(missing_ok=True)
