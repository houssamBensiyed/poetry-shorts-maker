"""Bounded retries for transient errors only; never log credentials or headers."""
import logging
import socket
import time
from google.auth import load_credentials_from_file
from googleapiclient.errors import HttpError
from gspread.exceptions import APIError
from requests.exceptions import Timeout as RequestsTimeout, ConnectionError as RequestsConnectionError
from app.video_builder import check_cancel

log = logging.getLogger('video_builder')
SCOPES = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive']


def credentials(config):
    return load_credentials_from_file(config['credentials_file'],scopes=SCOPES)[0]


def retry(call, stop=None, attempts=4):
    for attempt in range(attempts):
        if stop is not None:
            check_cancel(stop)
        try:
            return call()
        except (HttpError,APIError,TimeoutError,ConnectionError,socket.timeout,RequestsTimeout,RequestsConnectionError) as exc:
            if isinstance(exc,HttpError):
                status = int(exc.resp.status)
            elif isinstance(exc,APIError):
                status = exc.response.status_code
            else:
                status = 503
            if status not in {408,429,500,502,503,504} or attempt == attempts-1:
                raise
            delay = 2**attempt
            log.warning('Temporary Google request failure (%s); retrying in %ss',status,delay)
            if stop is None:
                time.sleep(delay)
            elif stop.wait(delay):
                check_cancel(stop)
