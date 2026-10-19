import threading
from pathlib import Path
from types import SimpleNamespace
import uuid
from app.config import load_config
from app.processor import Processor
from app.sheets import Job
from app.timeline import Word, Source
from app.transcription import Transcript
from app.video_builder import Cancelled


def setup(monkeypatch,tmp_path):
    c=load_config()
    c['google']['spreadsheet_id']=str(uuid.uuid4())
    c['paths'].update(temp=str(tmp_path/'temp'),output=str(tmp_path/'output'))
    monkeypatch.setattr('app.processor.check_job_config',lambda c:None)
    monkeypatch.setattr('app.processor.discover',lambda c:[Source('unused','image')])
    monkeypatch.setattr('app.processor.audio_duration',lambda p:2)
    updates=[]
    class FakeSheet:
        def __init__(self,*a): pass
        def jobs(self):
            return [Job(2,'duplicate','broken','NOT_PROCESSED'),Job(3,'duplicate','valid','NOT_PROCESSED'),Job(4,'done','valid','COMPLETED')]
        def update(self,job,status,**kwargs): updates.append((job.row,status,kwargs))
    class FakeDrive:
        def __init__(self,*a): pass
        def download(self,link,path):
            assert link in {'broken','valid'}, 'Downloader must receive the Sheet row link'
            if link=='broken': raise ValueError('Bad voiceover')
            Path(path).write_bytes(b'audio')
        def upload(self,path): return 'https://drive.google.com/file/d/example/view'
    def render(plan,audio,output,work,c,stop,progress):
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_bytes(b'output')
        progress(100)
    monkeypatch.setattr('app.processor.Sheets',FakeSheet)
    monkeypatch.setattr('app.processor.Drive',FakeDrive)
    monkeypatch.setattr('app.processor.render',render)
    p=Processor(c)
    p.transcriber=SimpleNamespace(transcribe=lambda *args:Transcript((Word('Hello.',0,1),),()))
    return p,updates


def test_failure_does_not_stop_next_job(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    assert p.start()
    p.thread.join(timeout=5)
    assert not p.thread.is_alive()
    assert [(row,status) for row,status,_ in updates]==[(2,'PROCESSING'),(2,'FAILED'),(3,'PROCESSING'),(3,'COMPLETED')]
    status=p.status()
    assert status['failed']==1 and status['completed']==2 and status['pending']==0
    assert status['status']=='idle'
    assert len(list((tmp_path/'temp').iterdir()))==1, 'Only failed job diagnostics remain'


def test_cancel_is_recoverable_and_stops_queue(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    def cancel(*args):
        p.stop_event.set()
        raise Cancelled('Interrupted; retry manually')
    monkeypatch.setattr('app.processor.Drive.download',cancel)
    assert p.start()
    p.thread.join(timeout=5)
    assert [(row,status) for row,status,_ in updates]==[(2,'PROCESSING'),(2,'FAILED')]
    assert p.status()['stage']=='Stopped'
    assert not p.run_lock.busy()


def test_upload_url_preserved_when_sheet_commit_fails(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    original=__import__('app.processor',fromlist=['Sheets']).Sheets.update
    def update(self,job,status,**kwargs):
        if status=='COMPLETED': raise ValueError('Sheet unavailable')
        return original(self,job,status,**kwargs)
    monkeypatch.setattr('app.processor.Sheets.update',update)
    p.start(); p.thread.join(timeout=5)
    assert updates[-1][1]=='FAILED'
    assert updates[-1][2]['output'].startswith('https://drive.google.com/')
    assert list((tmp_path/'output').glob('*.mp4'))
    assert list((tmp_path/'temp').glob('*/uploaded.json'))
