from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from app.config import load_config
from app.jobs import Job
from app.processor import Processor
from app.transcription import Transcript
from app.timeline import Word
from app.footage import probe


def test_audio_and_selected_image_to_local_render_then_upload(monkeypatch,tmp_path,file_server):
    c=load_config()
    c['baserow']['table_id']=91872
    c['paths'].update(buffer=str(tmp_path/'buffer'),output=str(tmp_path/'output'),temp=str(tmp_path/'temp'),image_buffer=str(tmp_path/'buffer-images'))
    c['video'].update(width=320,height=480,fps=20,preset='ultrafast')
    c['captions'].update(font_size=30,safe_margin=20)
    row={'status':'NOT_PROCESSED','output':'','uploaded_url':''}
    class FakeTable:
        def __init__(self,*args):pass
        def jobs(self):return [Job(44,'clip-1',file_server[0]+'/redirect',row['status'])]
        def image_urls(self):return [file_server[0]+'/image1',file_server[0]+'/image2',file_server[0]+'/image3']
        def update(self,job,status,output='',uploaded_url='',**kwargs):row.update(status=status,output=output,uploaded_url=uploaded_url)
    monkeypatch.setattr('app.processor.JobTable',FakeTable)
    monkeypatch.setattr('app.processor.check_job_config',lambda config:None)
    class FakeUploader:
        def __init__(self,*args):pass
        def public_id(self,path):return 'test-video'
        def upload(self,path,progress):
            assert Path(path).is_file() and row['status']=='PROCESSING'
            return {'url':'https://res.cloudinary.com/test/video.mp4','public_id':'test-video','bytes':Path(path).stat().st_size}
    monkeypatch.setattr('app.processor.Uploader',FakeUploader)
    processor=Processor(c)
    processor.transcriber=SimpleNamespace(transcribe=lambda *args:Transcript((Word('Hello.',.1,.7),),()))
    assert processor.start()
    processor.thread.join(timeout=20)
    assert not processor.thread.is_alive()
    assert row['status']=='DONE',processor.status()
    assert row['uploaded_url']=='https://res.cloudinary.com/test/video.mp4'
    images=list((tmp_path/'buffer-images').glob('*/*.png'))
    assert len(images)==1, 'Download only the randomly selected image, not the whole table'
    with Image.open(images[0]) as image: assert image.size==(160,120)
    output=Path(row['output'])
    assert output.is_file()
    assert abs(float(probe(output)['format']['duration'])-1)<.05
    buffered=list((tmp_path/'buffer').glob('*.audio'))
    assert len(buffered)==1 and buffered[0].read_bytes()==file_server[1]
    assert not list((tmp_path/'temp').iterdir())
    # A second run does not download/render an already completed row.
    processor.start();processor.thread.join(timeout=20)
    assert len(list((tmp_path/'buffer').glob('*.audio')))==1
    assert len(list((tmp_path/'output').glob('*.mp4')))==1
