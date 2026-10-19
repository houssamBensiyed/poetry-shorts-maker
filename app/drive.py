from pathlib import Path
import re
from urllib.parse import urlparse, parse_qs
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from app.google_client import credentials, retry
from app.video_builder import check_cancel


def file_id(link):
    if re.fullmatch(r'[A-Za-z0-9_-]{10,}',link):
        return link
    parsed = urlparse(link)
    if parsed.scheme!='https' or parsed.hostname not in {'drive.google.com','docs.google.com'}:
        raise ValueError('Use a Google Drive HTTPS file link or file ID')
    match = re.search(r'/file/d/([A-Za-z0-9_-]+)',parsed.path)
    result = match.group(1) if match else parse_qs(parsed.query).get('id',[''])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,}',result):
        raise ValueError('Google Drive link does not contain a valid file ID')
    return result


class Drive:
    def __init__(self, config, stop):
        http = google_auth_httplib2.AuthorizedHttp(credentials(config),http=httplib2.Http(timeout=30))
        self.service = build('drive','v3',http=http,cache_discovery=False)
        self.folder = config['output_drive_folder_id']
        self.stop = stop

    def download(self, link, destination):
        request = self.service.files().get_media(fileId=file_id(link),supportsAllDrives=True)
        try:
            with Path(destination).open('wb') as target:
                downloader = MediaIoBaseDownload(target,request,chunksize=1024*1024)
                done = False
                while not done:
                    _,done = retry(lambda:downloader.next_chunk(num_retries=0),self.stop)
            if Path(destination).stat().st_size == 0:
                raise ValueError('Downloaded voiceover is empty')
        except Exception:
            Path(destination).unlink(missing_ok=True)
            raise
        return destination

    def upload(self, path):
        check_cancel(self.stop)
        media = MediaFileUpload(str(path),mimetype='video/mp4',resumable=True,chunksize=4*1024*1024)
        request = self.service.files().create(body={'name':Path(path).name,'parents':[self.folder]},
            media_body=media,fields='id,webViewLink',supportsAllDrives=True)
        response = None
        while response is None:
            _,response = retry(lambda:request.next_chunk(num_retries=0),self.stop)
        # No permission mutation: inherit the configured folder's access policy.
        return response.get('webViewLink') or f'https://drive.google.com/file/d/{response["id"]}/view'
