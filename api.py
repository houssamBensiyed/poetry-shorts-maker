"""Optional HTTP entry point: python api.py. No scheduler, no automatic runs."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import os
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import JSONResponse
import uvicorn
from app.config import load_config
from app.logger import configure
from app.processor import Processor


def create_app(config=None, processor=None):
    config = config or load_config()
    key = os.environ.get('VIDEO_BUILDER_API_KEY','')
    if not key:
        raise ValueError('Set VIDEO_BUILDER_API_KEY in your environment before starting the API')
    processor = processor or Processor(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(processor.close)

    app = FastAPI(title='AI Video Builder',lifespan=lifespan)
    app.state.processor = processor

    def authenticate(x_api_key: str|None=Header(default=None)):
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(),key.encode()):
            raise HTTPException(status_code=401,detail='Invalid or missing X-API-Key header')

    @app.get('/run',dependencies=[Depends(authenticate)])
    def run():
        if not processor.start():
            return JSONResponse({'status':'already_running'},headers={'Cache-Control':'no-store'})
        return JSONResponse({'status':'accepted','message':'Video processing started'},status_code=202,
                            headers={'Cache-Control':'no-store'})

    @app.get('/status',dependencies=[Depends(authenticate)])
    def status():
        return JSONResponse(processor.status(),headers={'Cache-Control':'no-store'})

    return app


def main():
    try:
        config = load_config()
        configure(config['paths']['logs'])
        app = create_app(config)
    except (ValueError,OSError) as exc:
        raise SystemExit(f'Configuration error: {exc}') from exc
    uvicorn.run(app,host=os.environ.get('VIDEO_BUILDER_HOST',config['server']['host']),
                port=int(os.environ.get('VIDEO_BUILDER_PORT',config['server']['port'])))


if __name__=='__main__':
    main()
