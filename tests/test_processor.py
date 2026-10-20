import threading
from pathlib import Path
from types import SimpleNamespace
import uuid
from app.config import load_config
from app.processor import Processor
from app.jobs import Job
from app.timeline import Word, Source
from app.transcription import Transcript
from app.video_builder import Cancelled


def setup(monkeypatch,tmp_path):
    c=load_config()
    c['baserow']['table_id']=uuid.uuid4().int
    c['paths'].update(temp=str(tmp_path/'temp'),output=str(tmp_path/'output'),buffer=str(tmp_path/'buffer'),image_buffer=str(tmp_path/'images'))
    monkeypatch.setattr('app.processor.check_job_config',lambda c:None)
    monkeypatch.setattr('app.processor.download_images',lambda urls,*args:[Source('unused','image') for url in urls])
    monkeypatch.setattr('app.processor.audio_duration',lambda p:2)
    updates=[]
    class FakeTable:
        def __init__(self,*a): pass
        def jobs(self):
            return [Job(2,'duplicate','broken','NOT_PROCESSED'),Job(3,'duplicate','valid','NOT_PROCESSED'),Job(4,'done','valid','COMPLETED')]
        def image_urls(self): return ['https://example.test/background.png']
        def update(self,job,status,**kwargs): updates.append((job.row,status,kwargs))
    class FakeDownloader:
        def __init__(self,*a): pass
        def download(self,link,path):
            assert link in {'broken','valid'}, 'Downloader must receive the Sheet row link'
            if link=='broken': raise ValueError('Bad voiceover')
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            Path(path).write_bytes(b'audio')
    def render(plan,audio,output,work,c,stop,progress):
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_bytes(b'output')
        progress(100)
    monkeypatch.setattr('app.processor.JobTable',FakeTable)
    monkeypatch.setattr('app.processor.Downloader',FakeDownloader)
    monkeypatch.setattr('app.processor.render',render)
    class FakeUploader:
        def __init__(self,*args):pass
        def public_id(self,path):return Path(path).stem
        def upload(self,path,progress):return {'url':'https://res.cloudinary.com/example/video/upload/video.mp4','public_id':Path(path).stem,'bytes':Path(path).stat().st_size}
    monkeypatch.setattr('app.processor.Uploader',FakeUploader)
    p=Processor(c)
    p.transcriber=SimpleNamespace(transcribe=lambda *args:Transcript((Word('Hello.',0,1),),()))
    return p,updates


def test_failure_does_not_stop_next_job(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    assert p.start()
    p.thread.join(timeout=5)
    assert not p.thread.is_alive()
    assert [(row,status) for row,status,_ in updates]==[(2,'PROCESSING'),(2,'FAILED'),(3,'PROCESSING'),(3,'DONE')]
    status=p.status()
    assert status['failed']==1 and status['completed']==2 and status['pending']==0
    assert status['status']=='idle'
    assert len(list((tmp_path/'temp').iterdir()))==1, 'Only failed job diagnostics remain'
    assert len(list((tmp_path/'buffer').glob('*.audio')))==1, 'Successful audio remains in the local buffer'


def test_cancel_is_recoverable_and_stops_queue(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    def cancel(*args):
        p.stop_event.set()
        raise Cancelled('Interrupted; retry manually')
    monkeypatch.setattr('app.processor.Downloader.download',cancel)
    assert p.start()
    p.thread.join(timeout=5)
    assert [(row,status) for row,status,_ in updates]==[(2,'PROCESSING'),(2,'FAILED')]
    assert p.status()['stage']=='Stopped'
    assert not p.run_lock.busy()


def test_complete_output_preserved_when_table_commit_fails(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    original=__import__('app.processor',fromlist=['JobTable']).JobTable.update
    def update(self,job,status,**kwargs):
        if status=='DONE': raise ValueError('Sheet unavailable')
        return original(self,job,status,**kwargs)
    monkeypatch.setattr('app.processor.JobTable.update',update)
    p.start(); p.thread.join(timeout=5)
    assert updates[-1][1]=='FAILED'
    assert Path(updates[-1][2]['output']).is_file()
    assert list((tmp_path/'output').glob('*.mp4'))
    assert list((tmp_path/'temp').glob('*/uploaded.json'))


def test_upload_failure_never_marks_done_and_retains_local_video(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    def fail(*args):raise RuntimeError('Upload unavailable')
    monkeypatch.setattr('app.processor.Uploader.upload',fail)
    p.start();p.thread.join(timeout=5)
    assert not any(status=='DONE' for _,status,_ in updates)
    assert updates[-1][1]=='FAILED' and updates[-1][2]['uploaded_url']==''
    assert Path(updates[-1][2]['output']).is_file()
    assert list((tmp_path/'temp').glob('*/upload.json'))


def test_confirmed_upload_survives_stop_before_status_commit(monkeypatch,tmp_path):
    p,updates=setup(monkeypatch,tmp_path)
    def complete(self,path,progress):
        p.stop_event.set()
        return {'url':'https://res.cloudinary.com/demo/video.mp4','public_id':'test','bytes':6}
    monkeypatch.setattr('app.processor.Uploader.upload',complete)
    p.start();p.thread.join(timeout=5)
    assert updates[-1][1]=='DONE'
    assert updates[-1][2]['uploaded_url']=='https://res.cloudinary.com/demo/video.mp4'
