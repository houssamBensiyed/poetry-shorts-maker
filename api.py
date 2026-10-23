"""Expose authenticated, explicitly triggered processing through FastAPI.

Run with python api.py. GET /run starts the same Processor used by the dashboard;
GET /status reports its snapshot. Both require X-API-Key and disable caching.
There is no scheduler or automatic run, and application shutdown joins the local
worker so unfinished work reaches its cooperative cancellation checkpoints.
"""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from app.config import load_config
from app.logger import configure
from app.processor import Processor


def create_app(config=None, processor=None):
    """Build an authenticated API around an injected or default processor.

    Args:
        config: Optional loaded settings; omitted loads the project configuration.
        processor: Optional Processor-compatible object, useful for custom provider
            compositions and tests. Omitted constructs the default coordinator.

    Require VIDEO_BUILDER_API_KEY at construction and capture it for subsequent
    requests. Return a FastAPI app with the processor exposed on app.state.
    No processing starts until an authenticated client calls /run.
    """
    config = config or load_config()
    key = os.environ.get("VIDEO_BUILDER_API_KEY", "")
    if not key:
        raise ValueError("Set VIDEO_BUILDER_API_KEY in your environment before starting the API")
    processor = processor or Processor(config)

    @asynccontextmanager
    async def lifespan(app):
        """Yield application lifetime, then stop/join the worker off the async loop.

        Processor.close may wait for network timeouts or encoder cleanup, so move
        that blocking shutdown into a thread rather than blocking the event loop.
        """
        try:
            yield
        finally:
            await asyncio.to_thread(processor.close)

    app = FastAPI(title="AI Video Builder", lifespan=lifespan)
    app.state.processor = processor

    def authenticate(x_api_key: str | None = Header(default=None)):
        """Require a constant-time comparison against the captured API key.

        Read X-API-Key through FastAPI's header dependency and return HTTP 401 for
        missing/mismatched values without exposing the expected secret.
        """
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), key.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

    @app.get("/run", dependencies=[Depends(authenticate)])
    def run(mode: Literal["image", "video", "hybrid"] | None = None):
        """Request a manual run and return an uncached acknowledgement.

        HTTP 202 means a worker was started, not that jobs finished successfully.
        A refused start returns already_running; clients use /status to monitor work.
        Because this GET has a side effect, it is intended for explicit API triggers.
        """
        if not (processor.start() if mode is None else processor.start(mode=mode)):
            return JSONResponse(
                {"status": "already_running"}, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {"status": "accepted", "message": "Video processing started"},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/status", dependencies=[Depends(authenticate)])
    def status():
        """Return an uncached local or externally published processor snapshot."""
        return JSONResponse(processor.status(), headers={"Cache-Control": "no-store"})

    return app


def main():
    """Load settings, configure logs and serve the API with optional bind overrides.

    VIDEO_BUILDER_HOST and VIDEO_BUILDER_PORT override YAML server values. Convert
    setup errors to a concise SystemExit message; actual job/provider validation
    remains deferred until an authenticated run request starts the worker.
    """
    try:
        config = load_config()
        configure(config["paths"]["logs"])
        app = create_app(config)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    uvicorn.run(
        app,
        host=os.environ.get("VIDEO_BUILDER_HOST", config["server"]["host"]),
        port=int(os.environ.get("VIDEO_BUILDER_PORT", config["server"]["port"])),
    )


if __name__ == "__main__":
    main()
