import json
import threading
import httpx
import pytest
from app.jobs import JobTable, Job


def table(monkeypatch, handler, stop=None):
    monkeypatch.setenv('BASEROW_API_TOKEN','test-only')
    factory=httpx.Client
    monkeypatch.setattr('app.jobs.httpx.Client',lambda **kw:factory(transport=httpx.MockTransport(handler),**kw))
    return JobTable({'api_url':'https://api.baserow.io','table_id':123,'images_table_id':456,'api_key_env':'BASEROW_API_TOKEN'},stop)


def record(row, status):
    return {'id':row,'ID':'duplicate','Audio URL':f'https://files.example/{row}.wav',
            'Status':status,'Output File':'','Output URL':'','Error':''}


def test_paginated_jobs_and_update_use_real_row_id(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.headers['Authorization']=='Token test-only'
        assert request.url.params['user_field_names']=='true'
        if request.method=='PATCH':
            assert request.url.path=='/api/database/rows/table/123/99/'
            assert json.loads(request.content)=={'Status':'DONE','Output File':'output/video.mp4','Output URL':'https://files.example/video.mp4','Error':''}
            return httpx.Response(200,json={})
        if request.url.params['page']=='1':
            return httpx.Response(200,json={'results':[record(42,'NOT_PROCESSED')],'next':'next page'})
        return httpx.Response(200,json={'results':[record(99,{'id':2,'value':'COMPLETED'})],'next':None})
    source=table(monkeypatch,handler)
    jobs=source.jobs()
    assert [(j.row,j.status) for j in jobs]==[(42,'NOT_PROCESSED'),(99,'COMPLETED')]
    assert jobs[0].voice_link=='https://files.example/42.wav'
    source.update(jobs[1],'DONE',output='output/video.mp4',uploaded_url='https://files.example/video.mp4')
    assert len(requests)==3


def test_bounded_retries_do_not_retry_unauthorized(monkeypatch):
    calls=[]
    source=table(monkeypatch,lambda request:(calls.append(request) or httpx.Response(401,json={})))
    with pytest.raises(ValueError,match='401'): source.jobs()
    assert len(calls)==1


def test_temporary_failures_have_bounded_retries(monkeypatch):
    calls=[]
    monkeypatch.setattr('time.sleep',lambda _:None)
    source=table(monkeypatch,lambda request:(calls.append(request) or httpx.Response(503,json={})))
    with pytest.raises(RuntimeError,match='4 attempts'): source.jobs()
    assert len(calls)==4


def test_missing_columns_fail_clearly(monkeypatch):
    source=table(monkeypatch,lambda request:httpx.Response(200,json={'results':[{'id':42}],'next':None}))
    with pytest.raises(ValueError,match='Audio URL'): source.jobs()


def test_recovery_update_works_after_cancellation(monkeypatch):
    stop=threading.Event(); stop.set()
    calls=[]
    source=table(monkeypatch,lambda request:(calls.append(request) or httpx.Response(200,json={})),stop)
    source.update(Job(42,'job','https://files.example/audio.wav','PROCESSING'),'FAILED',error='Cancelled')
    assert len(calls)==1


def test_images_come_from_separate_table_and_are_deduplicated(monkeypatch):
    def handler(request):
        assert request.url.path=='/api/database/rows/table/456/'
        return httpx.Response(200,json={'next':None,'results':[
            {'id':1,'Image URL':'https://files.example/a.png'},
            {'id':2,'Image URL':'https://files.example/b.png'},
            {'id':3,'Image URL':'https://files.example/a.png'},
            {'id':4,'Image URL':''}]})
    source=table(monkeypatch,handler)
    assert source.image_urls()==['https://files.example/a.png','https://files.example/b.png']
