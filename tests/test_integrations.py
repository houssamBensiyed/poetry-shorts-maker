from types import SimpleNamespace
import threading
import uuid
import subprocess
import sys
import json
import pytest
from fastapi.testclient import TestClient
from app.config import load_config
from app.drive import file_id
from app.sheets import Sheets, Job
from app.state import RunLock
from app.transcription import Transcriber
from app.timeline import Word
from app.google_client import retry
from app.video_builder import Cancelled, run_ffmpeg
from api import create_app


@pytest.mark.parametrize('link',[
    'https://drive.google.com/file/d/1234567890abc/view?usp=sharing',
    'https://drive.google.com/open?id=1234567890abc',
    'https://drive.google.com/uc?export=download&id=1234567890abc',
    '1234567890abc'])
def test_drive_url(link):
    assert file_id(link)=='1234567890abc'

@pytest.mark.parametrize('link',['https://evil.example/file/d/1234567890abc','https://drive.google.com/drive/folders/foo','bad'])
def test_invalid_drive_url(link):
    with pytest.raises(ValueError): file_id(link)


def test_sheet_uses_row_positions_and_raw_values():
    class FakeWorksheet:
        def get_all_values(self):
            return [['Error','Status','ID','Output Link','Voice Drive Link'],
                    ['','NOT_PROCESSED','duplicate','','link-a'],[],
                    ['','COMPLETED','duplicate','','link-b']]
        def batch_update(self,body,**kwargs):
            self.body,self.kwargs=body,kwargs
    sheet = Sheets.__new__(Sheets)
    sheet.sheet,sheet.stop=FakeWorksheet(),None
    jobs = sheet.jobs()
    assert [j.row for j in jobs]==[2,4]
    sheet.update(jobs[1],'FAILED',error='=formula')
    assert sheet.sheet.body[0]['range']=='B4'
    assert sheet.sheet.body[2]['range']=='A4'
    assert sheet.sheet.kwargs['value_input_option']=='RAW'


def test_cross_process_lock_released():
    identity = str(uuid.uuid4())
    lock=RunLock(identity)
    assert lock.acquire()
    code = 'from app.state import RunLock; l=RunLock('+repr(identity)+'); print(l.acquire()); l.release()'
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='False'
    lock.release()
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='True'


def test_bounded_retry(monkeypatch):
    calls=[]
    monkeypatch.setattr('app.google_client.time.sleep',lambda _:None)
    def fail():
        calls.append(1)
        raise TimeoutError('temporary')
    with pytest.raises(TimeoutError): retry(fail)
    assert len(calls)==4


def test_api_auth_and_shared_processor(monkeypatch):
    monkeypatch.setenv('VIDEO_BUILDER_API_KEY','test-only-key')
    class FakeProcessor:
        active=False
        def start(self):
            if self.active: return False
            self.active=True
            return True
        def status(self): return {'status':'processing' if self.active else 'idle','render_progress':37}
        def close(self): self.active=False
    processor=FakeProcessor()
    with TestClient(create_app(processor=processor)) as client:
        assert client.get('/run').status_code==401
        assert client.get('/status').status_code==401
        headers={'X-API-Key':'test-only-key'}
        assert client.get('/run',headers=headers).status_code==202
        assert client.get('/run',headers=headers).json()['status']=='already_running'
        assert client.get('/status',headers=headers).json()['render_progress']==37
    assert not processor.active


def test_api_requires_key(monkeypatch):
    monkeypatch.delenv('VIDEO_BUILDER_API_KEY',raising=False)
    with pytest.raises(ValueError,match='VIDEO_BUILDER_API_KEY'): create_app()


def test_ffmpeg_cancellation(tmp_path):
    stop=threading.Event()
    timer=threading.Timer(.3,stop.set)
    timer.start()
    try:
        with pytest.raises(Cancelled):
            run_ffmpeg(['-re','-f','lavfi','-i','color=s=64x64:r=10','-f','null','-'],tmp_path,stop,100,lambda p:None,'Cancellation test')
    finally:
        timer.cancel()
