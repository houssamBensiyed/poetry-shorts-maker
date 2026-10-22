"""Adapt Baserow rows to job persistence and the reusable image catalog.

Requests use user field names for the documented schema, while updates address
Baserow's internal numeric row IDs. Editable ID labels and table positions never
identify writes. Authentication and bounded retries stay inside this adapter so
application services depend only on repository and catalog contracts.
"""

import logging
import os

import httpx

from app.cancellation import check_cancel
from app.models import Job as Job

log = logging.getLogger("video_builder")
STATUSES = {"NOT_PROCESSED", "PROCESSING", "COMPLETED", "DONE", "FAILED"}
FIELDS = ("ID", "Audio URL", "Status", "Output File", "Output URL", "Error")


class JobTable:
    """Read audio/image tables and acknowledge lifecycle updates through Baserow.

    A single adapter supports both tables in the default composition. STATUSES
    includes legacy COMPLETED; new successful work writes DONE. FIELDS lists
    the required editable audio-table columns.
    """

    def __init__(self, config, stop=None):
        """Read the token and construct the audio-table endpoint without network I/O.

        config supplies api_url, table_id, images_table_id and api_key_env. stop is
        used for table reads; request/update accept their own optional event so
        recovery and completion can deliberately proceed after cancellation.
        """
        self.config, self.stop = config, stop
        if not config["table_id"]:
            raise ValueError("Set baserow.table_id in config.yaml")
        self.token = os.environ.get(config["api_key_env"], "")
        if not self.token:
            raise ValueError(f"Set {config['api_key_env']} securely in the environment")
        self.url = config["api_url"].rstrip("/") + f"/api/database/rows/table/{config['table_id']}/"

    def request(self, method, url, stop=None, **kwargs):
        """Send an authenticated request and return decoded successful JSON.

        method/url select the Baserow operation; kwargs are forwarded to httpx.
        Use a 30-second timeout and disable redirects. Retry transport failures and
        selected transient HTTP statuses up to four total attempts. stop controls
        only this call, not self.stop: None allows a recovery write to finish even
        when a run was cancelled. Error messages omit credentials and response bodies.

        Raise ValueError for permanent HTTP failures or RuntimeError when retries
        are exhausted. JSON decoding errors also propagate to the calling policy.
        """
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for attempt in range(4):
                if stop is not None:
                    check_cancel(stop)
                try:
                    response = client.request(
                        method, url, headers={"Authorization": f"Token {self.token}"}, **kwargs
                    )
                    if response.is_success:
                        return response.json()
                    if response.status_code not in {408, 429, 500, 502, 503, 504}:
                        raise ValueError(
                            f"Baserow returned HTTP {response.status_code}; check table ID, database token, permissions and field names"
                        )
                    failure = f"HTTP {response.status_code}"
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    failure = type(exc).__name__
                if attempt == 3:
                    raise RuntimeError(f"Baserow request failed after 4 attempts ({failure})")
                log.warning("Temporary Baserow failure (%s); retrying", failure)
                if stop is None:
                    import time

                    time.sleep(2**attempt)
                else:
                    stop.wait(2**attempt)

    def rows(self, table_id):
        """Yield rows from the requested numeric table ID in pages of 200.

        Build page requests against the configured server rather than following
        returned pagination URLs with the token. Reject duplicate row IDs, empty
        continuation pages and more than 10,000 pages to avoid silent duplication
        or endless traversal. This is not a transactionally consistent remote snapshot.
        """
        seen = set()
        url = self.config["api_url"].rstrip("/") + f"/api/database/rows/table/{table_id}/"
        for page in range(1, 10001):
            data = self.request(
                "GET",
                url,
                self.stop,
                params={"user_field_names": "true", "size": 200, "page": page},
            )
            for row in data["results"]:
                row_id = int(row["id"])
                if row_id in seen:
                    raise ValueError("Baserow rows changed during pagination; refresh the job list")
                seen.add(row_id)
                yield row
            if not data.get("next"):
                return
            if not data["results"]:
                raise ValueError("Baserow returned empty pagination data")
        raise ValueError("Baserow table exceeds the supported pagination limit")

    def jobs(self):
        """Return audio-table rows as Job values after checking required fields.

        Normalize text and single-select statuses. Preserve unknown statuses with a
        warning; the coordinator counts/selects recognized values and skips the rest.
        Read all rows once to form the run's scheduling and display-count snapshot.
        """
        jobs = []
        for row in self.rows(self.config["table_id"]):
            if any(field not in row for field in FIELDS):
                raise ValueError("Baserow audio table needs these fields: " + ", ".join(FIELDS))
            row_id = int(row["id"])
            status = row["Status"]
            if isinstance(status, dict):
                status = status.get("value", "")
            status = str(status or "").strip()
            if status not in STATUSES:
                log.warning("Baserow row %s has an unrecognized status and will be skipped", row_id)
            jobs.append(
                Job(row_id, str(row["ID"] or ""), str(row["Audio URL"] or "").strip(), status)
            )
        log.info("Read %d audio jobs from Baserow", len(jobs))
        return jobs

    def image_urls(self):
        """Return distinct usable image links in first-seen order.

        Require the Image URL column, skip syntactically invalid links with row-based
        warnings, and fail when no usable candidates remain. Actual image bytes are
        downloaded and decoded only after planning selects them.
        """
        from app.downloads import validate_url

        urls, total = [], 0
        for row in self.rows(self.config["images_table_id"]):
            total += 1
            if "Image URL" not in row:
                raise ValueError("Baserow image table needs an Image URL field")
            url = str(row["Image URL"] or "").strip()
            try:
                validate_url(url)
            except ValueError:
                log.warning("Skipping image row %s: missing or invalid HTTP/HTTPS link", row["id"])
                continue
            urls.append(url)
        urls = list(dict.fromkeys(urls))
        log.info(
            "Image table contains %d rows and %d distinct usable image links", total, len(urls)
        )
        if not urls:
            raise ValueError("No usable image links in the Baserow image table")
        return urls

    def update(self, job, status, output="", error="", stop=None, uploaded_url=""):
        """PATCH lifecycle fields on job.row and wait for acknowledgement.

        status must belong to STATUSES. output and uploaded_url identify a completed
        local file and confirmed remote upload; error is limited to 1,000 characters.
        All result fields are written, including empty defaults, so PROCESSING clears
        stale results. Pass stop for interruptible writes; omit it for DONE/FAILED so
        cancellation cannot prevent completion or a recovery attempt.
        """
        if status not in STATUSES:
            raise ValueError("Invalid job status")
        # Use Baserow's stable row ID, never the editable ID label or table position.
        self.request(
            "PATCH",
            self.url + f"{job.row}/",
            stop,
            params={"user_field_names": "true"},
            json={
                "Status": status,
                "Output File": output,
                "Output URL": uploaded_url,
                "Error": error[:1000],
            },
        )
