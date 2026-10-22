"""Publish videos through signed, bounded-retry Cloudinary chunk uploads.

The intended public ID is derived before transfer and checkpointed by JobRunner.
Each chunk shares an upload session ID and byte-range headers. A successful HTTP
response alone is insufficient: the final response must confirm the expected
video, public ID, byte count and public HTTPS URL before completion is reported.
"""

import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cloudinary.utils import sign_request

from app.cancellation import check_cancel

log = logging.getLogger("video_builder")


class Uploader:
    """Upload completed local MP4s while keeping credentials inside the adapter."""

    def __init__(self, config, stop):
        """Load Cloudinary credentials and retain the shared stop event.

        config provides cloud_name, environment-variable names, folder, chunk_mb and
        timeout_seconds. Raise ValueError for missing account/key/secret; no upload
        request is made during construction.
        """
        self.config, self.stop = config, stop
        self.api_key = os.environ.get(config["api_key_env"], "")
        self.api_secret = os.environ.get(config["api_secret_env"], "")
        if not config["cloud_name"] or not self.api_key or not self.api_secret:
            raise ValueError(
                "Configure the Cloudinary cloud name, API key and API secret before uploading"
            )

    def public_id(self, path):
        """Return the configured folder prefix plus the local filename stem.

        No network work occurs. Workspace filenames include a per-attempt UUID,
        yielding a stable intended ID for that attempt without relying on display labels.
        """
        prefix = self.config["folder"].strip("/")
        return f"{prefix}/{Path(path).stem}" if prefix else Path(path).stem

    def upload(self, path, progress=lambda value: None):
        """Upload a nonempty MP4 and return its confirmed URL, public ID and bytes.

        Args:
            path: Completed local video, read in configured MiB-sized chunks.
            progress: Percentage callback; 100 is emitted only after final validation.

        Sign each request with Cloudinary's helper and overwrite=false. Retry each
        chunk up to four times on transport or selected transient HTTP failures,
        retaining the same session ID and byte range. Require done=false from
        intermediate responses, then validate final identity, type, size and HTTPS URL.

        Check cancellation before chunks and attempts, but preserve a final confirmed
        result when Stop arrives with its response so JobRunner can commit DONE.
        Raise on incomplete or ambiguous completion; inspect upload.json before a
        manual retry because a remote object may exist despite a local error.
        """
        path = Path(path)
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("Cannot upload an empty video")
        public_id = self.public_id(path)
        upload_id = uuid.uuid4().hex
        url = f"https://api.cloudinary.com/v1_1/{self.config['cloud_name']}/video/upload"
        chunk_size = self.config["chunk_mb"] * 1024 * 1024
        result = None
        with (
            path.open("rb") as source,
            httpx.Client(timeout=self.config["timeout_seconds"], follow_redirects=False) as client,
        ):
            offset = 0
            while chunk := source.read(chunk_size):
                check_cancel(self.stop)
                headers = {
                    "X-Unique-Upload-Id": upload_id,
                    "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                }
                for attempt in range(4):
                    check_cancel(self.stop)
                    # Use Cloudinary's official signing helper; the raw secret never leaves the process.
                    fields = sign_request(
                        {
                            "timestamp": str(int(time.time())),
                            "public_id": public_id,
                            "overwrite": "false",
                            "type": "upload",
                        },
                        {"api_key": self.api_key, "api_secret": self.api_secret},
                    )
                    try:
                        response = client.post(
                            url,
                            headers=headers,
                            data=fields,
                            files={"file": (path.name, chunk, "video/mp4")},
                        )
                        if response.status_code == 200:
                            result = response.json()
                            if not isinstance(result, dict):
                                raise ValueError("Cloudinary returned invalid upload data")
                            break
                        if response.status_code not in {408, 429, 500, 502, 503, 504}:
                            raise ValueError(
                                f"Cloudinary returned HTTP {response.status_code}; check credentials, account limits and the upload checkpoint"
                            )
                        failure = f"HTTP {response.status_code}"
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        failure = type(exc).__name__
                    if attempt == 3:
                        raise RuntimeError(
                            f"Cloudinary upload failed after 4 attempts ({failure}); inspect the public ID in upload.json before retrying"
                        )
                    log.warning("Temporary Cloudinary failure (%s); retrying chunk", failure)
                    self.stop.wait(2**attempt)
                offset += len(chunk)
                if offset < size and result.get("done") is not False:
                    raise ValueError(
                        "Cloudinary returned an unexpected intermediate upload response"
                    )
                if offset < size:
                    progress(round(offset / size * 100, 1))
        secure_url = result.get("secure_url", "") if isinstance(result, dict) else ""
        parsed = urlparse(secure_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or result.get("public_id") != public_id
            or result.get("resource_type") != "video"
            or result.get("bytes") != size
            or result.get("done") is False
        ):
            raise ValueError(
                "Cloudinary did not confirm a complete video upload; inspect upload.json before retrying"
            )
        progress(100)
        # Do not discard a confirmed upload if Stop arrived with the final response.
        return {"url": secure_url, "public_id": public_id, "bytes": size}
