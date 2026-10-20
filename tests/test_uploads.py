import io
from email.parser import BytesParser
from email.policy import default
import hashlib
import threading
import httpx
import pytest
from app.config import load_config
from app.uploads import Uploader
from app.video_builder import Cancelled


def multipart(request):
    message=BytesParser(policy=default).parsebytes(
        ('Content-Type: '+request.headers['Content-Type']+'\r\n\r\n').encode()+request.content)
    return {part.get_param('name',header='Content-Disposition'):part.get_payload(decode=True)
            for part in message.iter_parts()}


def setup(monkeypatch,handler,stop=None):
    config=load_config()['cloudinary']
    config.update(cloud_name='test-cloud',chunk_mb=5)
    monkeypatch.setenv('CLOUDINARY_API_KEY','test-key')
    monkeypatch.setenv('CLOUDINARY_API_SECRET','test-secret')
    factory=httpx.Client
    monkeypatch.setattr('app.uploads.httpx.Client',lambda **kw:factory(transport=httpx.MockTransport(handler),**kw))
    return Uploader(config,stop or threading.Event())


def test_signed_chunk_upload_preserves_bytes_and_retries_same_chunk(monkeypatch,tmp_path):
    path=tmp_path/'video.mp4'; content=b'x'*(5*1024*1024)+b'last-bytes';path.write_bytes(content)
    seen=[];failures=[0]
    def handler(request):
        data=multipart(request)
        assert str(request.url)=='https://api.cloudinary.com/v1_1/test-cloud/video/upload'
        assert data['api_key']==b'test-key' and b'test-secret' not in request.content
        expected='&'.join(f'{k}={data[k].decode()}' for k in ['overwrite','public_id','timestamp','type'])+'test-secret'
        assert data['signature'].decode()==hashlib.sha1(expected.encode()).hexdigest()
        seen.append((request.headers['Content-Range'],request.headers['X-Unique-Upload-Id'],data['file']))
        if not failures[0]:
            failures[0]+=1
            return httpx.Response(503,json={})
        if len(data['file'])==5*1024*1024:
            return httpx.Response(200,json={'done':False,'public_id':'poetry-shorts/video'})
        return httpx.Response(200,json={'done':True,'secure_url':'https://res.cloudinary.com/test/video.mp4',
                                       'public_id':'poetry-shorts/video','resource_type':'video','bytes':len(content)})
    uploader=setup(monkeypatch,handler)
    progress=[]; result=uploader.upload(path,progress.append)
    assert result['url'].startswith('https://')
    assert seen[0]==seen[1], 'A failed chunk must be retried with the same upload ID and range'
    assert seen[1][2]+seen[2][2]==content
    assert seen[1][1]==seen[2][1] and progress[-1]==100


def test_unauthorized_upload_not_retried(monkeypatch,tmp_path):
    path=tmp_path/'video.mp4';path.write_bytes(b'video')
    calls=[]
    uploader=setup(monkeypatch,lambda r:(calls.append(r) or httpx.Response(401,json={})))
    with pytest.raises(ValueError,match='401'):uploader.upload(path)
    assert len(calls)==1 and path.read_bytes()==b'video'


def test_cancellation_between_chunks(monkeypatch,tmp_path):
    path=tmp_path/'video.mp4';path.write_bytes(b'x'*(5*1024*1024+1))
    stop=threading.Event();calls=[]
    def handler(request):
        calls.append(request);stop.set()
        return httpx.Response(200,json={'done':False})
    uploader=setup(monkeypatch,handler,stop)
    with pytest.raises(Cancelled):uploader.upload(path)
    assert len(calls)==1 and path.is_file()


def test_missing_final_confirmation_is_failure(monkeypatch,tmp_path):
    path=tmp_path/'video.mp4';path.write_bytes(b'video')
    uploader=setup(monkeypatch,lambda r:httpx.Response(200,json={'done':False}))
    with pytest.raises(ValueError,match='confirm'):uploader.upload(path)
