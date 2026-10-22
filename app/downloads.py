"""Buffer direct HTTP(S) files without a storage-provider SDK.

Streaming limits memory use and enforces the configured byte ceiling even if
Content-Length is missing or misleading. Transfers use sibling partial files so
downstream stages only see completed inputs. Error messages omit source URLs
because links may contain signed access tokens.
"""

import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.cancellation import check_cancel

log = logging.getLogger("video_builder")


def validate_url(url):
    """Require an HTTP(S) URL with a hostname and no embedded username/password.

    Raise ValueError on unsupported URL shapes. This is a syntax check only;
    it does not verify download access, content type or destination network.
    """
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Use an HTTP/HTTPS file URL without embedded username/password")


class Downloader:
    """Stream bounded downloads with cooperative cancellation and transient retries."""

    def __init__(self, config, stop):
        """Retain download settings and the run's shared cancellation event.

        config supplies timeout_seconds and max_file_mb. Construction performs no I/O.
        """
        self.config = config
        self.stop = stop

    def download(self, url, destination):
        """Download url into a new destination and return its completed Path.

        Create parent directories, reject existing buffered files, follow at most
        five redirects, and reject HTML/login pages or empty payloads. Stream in
        64 KiB chunks, checking cancellation and the size limit throughout. Retry
        transport failures and selected transient HTTP responses up to four total
        attempts with interruptible exponential waits.

        Rename the partial file only after completion and remove partial data on all
        exits. File contents are validated later by audio probing or image decoding.

        Raises:
            ValueError: Invalid URL, destination, HTTP response, content or size.
            RuntimeError: Retryable transfer failures exhaust four attempts.
            Cancelled: The shared event is observed before finalization.
            OSError: Local storage cannot be created or written.
        """
        validate_url(url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError(
                "Download destination already exists; refusing to overwrite the buffered file"
            )
        partial = destination.with_name(destination.name + ".partial")
        maximum = self.config["max_file_mb"] * 1024 * 1024
        try:
            with httpx.Client(
                timeout=self.config["timeout_seconds"], follow_redirects=True, max_redirects=5
            ) as client:
                for attempt in range(4):
                    check_cancel(self.stop)
                    try:
                        with client.stream("GET", url) as response:
                            if response.status_code in {408, 429, 500, 502, 503, 504}:
                                failure = f"HTTP {response.status_code}"
                            elif response.status_code != 200:
                                raise ValueError(
                                    f"File download returned HTTP {response.status_code}; check that the link grants download access"
                                )
                            else:
                                content_type = (
                                    response.headers.get("content-type", "").split(";")[0].lower()
                                )
                                if content_type in {"text/html", "application/xhtml+xml"}:
                                    raise ValueError(
                                        "The link returned a web/login page. Use a direct download link to the file."
                                    )
                                size = response.headers.get("content-length")
                                if size and size.isdigit() and int(size) > maximum:
                                    raise ValueError(
                                        f"File exceeds the {self.config['max_file_mb']} MB download limit"
                                    )
                                count = 0
                                with partial.open("wb") as target:
                                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                                        check_cancel(self.stop)
                                        count += len(chunk)
                                        if count > maximum:
                                            raise ValueError(
                                                f"File exceeds the {self.config['max_file_mb']} MB download limit"
                                            )
                                        target.write(chunk)
                                if count == 0:
                                    raise ValueError("Downloaded file is empty")
                                check_cancel(self.stop)
                                partial.replace(destination)
                                log.info(
                                    "Downloaded %s bytes to local buffer: %s",
                                    count,
                                    destination.name,
                                )
                                return destination
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        # URLs may contain signed access tokens. Do not include them in logs/errors.
                        failure = type(exc).__name__
                    partial.unlink(missing_ok=True)
                    if attempt == 3:
                        raise RuntimeError(f"File download failed after 4 attempts ({failure})")
                    log.warning("Temporary download failure (%s); retrying", failure)
                    self.stop.wait(2**attempt)
        finally:
            partial.unlink(missing_ok=True)
