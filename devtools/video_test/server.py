"""Serve a local testing UI without loading production jobs or publishers.

Uploads, outputs and diagnostics stay under the configured temporary directory.
Sessions live in memory; files and render diagnostics remain on disk for review.
Only one render runs at a time. Each run freezes its session's inputs until the
worker finishes, including cancellation. The launcher binds to loopback only.
"""

import asyncio
import copy
import json
import logging
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.cancellation import Cancelled, check_cancel
from app.config import (
    check_composition_config,
    check_render_config,
    check_transcription_config,
    load_config,
)
from app.footage import audio_duration
from app.timeline import Source, build_plan, sentences_from_words
from app.transcription import Transcriber
from app.video_builder import render

STATIC = Path(__file__).parent / "static"
IMAGE_LIMIT = 20 * 1024 * 1024
AUDIO_LIMIT = 100 * 1024 * 1024
log = logging.getLogger(__name__)


class RunOptions(BaseModel):
    """Bounded, per-run overrides; never write to the project's config.yaml."""

    model_config = ConfigDict(extra="forbid")

    size: Literal["preview", "full"] = "preview"
    seed: int = Field(default=42, ge=0, le=2147483647)


class Session:
    """Own buffered media and observable state for one browser session."""

    def __init__(self, config):
        self.id = uuid.uuid4().hex
        self.images = []
        self.audio = None
        self.lock = threading.RLock()
        self.uploading = False
        self.stop = threading.Event()
        self.thread = None
        self.directory = Path(config["paths"]["temp"]).resolve() / "ui-test" / self.id
        self.image_dir = self.directory / "images"
        self.audio_dir = self.directory / "voiceovers"
        self.run = None

    def run_directory(self, run_id):
        return self.directory / "runs" / run_id

    @property
    def active(self):
        return self.run is not None and self.run["status"] in {"running", "stopping"}

    def available(self):
        if self.active or self.uploading:
            raise HTTPException(409, "Wait for the current upload or test to finish.")

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(
                {
                    "id": self.id,
                    "images": self.images,
                    "audio": self.audio,
                    "run": self.run,
                    "uploading": self.uploading,
                }
            )

    def event(self, message, progress=None):
        with self.lock:
            self.run["events"].append({"time": time.time(), "message": message})
            self.run["stage"] = message
            if progress is not None:
                self.run["progress"] = progress


def create_app(config=None):
    """Create an isolated test service using the existing renderer and planner."""
    config = copy.deepcopy(config if config is not None else load_config())
    sessions = {}
    render_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        workers = []
        for session in sessions.values():
            session.stop.set()
            if session.thread is not None:
                workers.append(asyncio.to_thread(session.thread.join))
        if workers:
            await asyncio.gather(*workers)

    app = FastAPI(title="Video Builder Test", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.sessions = sessions
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_requests(request, call_next):
        # A custom header prevents cross-origin forms from writing to temporary sessions.
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if request.headers.get("x-video-test") != "1" or (origin and origin != expected):
                return JSONResponse(
                    {"detail": "Open the UI from this local server."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def get_session(session_id):
        if session_id not in sessions:
            raise HTTPException(404, "This session has ended. Reload to start a new test.")
        return sessions[session_id]

    def media_path(session, item):
        folder = session.image_dir if item["kind"] == "image" else session.audio_dir
        return folder / item["file"]

    @app.get("/api/info")
    def info():
        speech_error = None
        try:
            check_transcription_config(config)
        except ValueError as exc:
            speech_error = str(exc)
        assets_error = None
        try:
            check_composition_config(config)
        except ValueError as exc:
            assets_error = str(exc)
        return {
            "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "font": Path(config["captions"]["font_file"]).is_file(),
            "assets": assets_error is None,
            "assets_error": assets_error,
            "speech": speech_error is None,
            "speech_error": speech_error,
            "speech_model": config["whisper"]["model"],
            "full_size": f"{config['video']['width']} × {config['video']['height']}",
            "preview_size": f"360 × {max(2, round(config['video']['height'] * 360 / config['video']['width'] / 2) * 2)}",
            "fps": config["video"]["fps"],
        }

    @app.post("/api/sessions", status_code=201)
    def new_session():
        if len(sessions) >= 64:
            raise HTTPException(409, "Session limit reached. Restart the test server.")
        session = Session(config)
        sessions[session.id] = session
        return session.snapshot()

    @app.get("/api/sessions/{session_id}")
    def status(session_id: str):
        return get_session(session_id).snapshot()

    @app.post("/api/sessions/{session_id}/media/{kind}", status_code=201)
    async def upload(session_id: str, kind: Literal["image", "audio"], request: Request):
        session = get_session(session_id)
        with session.lock:
            session.available()
            if kind == "image" and len(session.images) >= 10:
                raise HTTPException(400, "A test session can hold up to 10 images.")
            session.uploading = True
        media_id = uuid.uuid4().hex
        folder = session.image_dir if kind == "image" else session.audio_dir
        pending = folder / f"{media_id}.upload"
        final = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = Path(request.query_params.get("name", "upload").replace("\\", "/")).name[:150]
            size, limit = 0, IMAGE_LIMIT if kind == "image" else AUDIO_LIMIT
            with pending.open("xb") as dest:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            413, f"File exceeds the {limit // 1024 // 1024} MB limit."
                        )
                    dest.write(chunk)
            if not size:
                raise HTTPException(400, "The file is empty.")
            item = {"id": media_id, "name": name, "kind": kind, "bytes": size}
            if kind == "image":
                final = folder / f"{media_id}.png"

                def decode():
                    with Image.open(pending) as source:
                        if source.format not in {"JPEG", "PNG", "WEBP"}:
                            raise ValueError("Use a JPG, PNG or WebP image.")
                        if source.width * source.height > 40_000_000:
                            raise ValueError("Images must have fewer than 40 million pixels.")
                        image = ImageOps.exif_transpose(source).convert("RGB")
                        image.save(final, "PNG")
                        return image.size

                item["width"], item["height"] = await asyncio.to_thread(decode)
            else:
                duration = await asyncio.to_thread(audio_duration, pending)
                if duration > 600:
                    raise ValueError("Use a voice recording shorter than 10 minutes.")
                item["duration"] = duration
                extension = Path(name).suffix.lower()
                if extension not in {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm"}:
                    extension = ".audio"
                final = folder / f"{media_id}{extension}"
                pending.replace(final)
            item["file"] = final.name
            with session.lock:
                if kind == "image":
                    session.images.append(item)
                else:
                    if session.audio:
                        media_path(session, session.audio).unlink(missing_ok=True)
                    session.audio = item
            return session.snapshot()
        except HTTPException:
            if final:
                final.unlink(missing_ok=True)
            raise
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            if final:
                final.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        finally:
            pending.unlink(missing_ok=True)
            with session.lock:
                session.uploading = False

    @app.get("/api/sessions/{session_id}/media/{media_id}")
    def media(session_id: str, media_id: str):
        session = get_session(session_id)
        with session.lock:
            items = session.images + ([session.audio] if session.audio else [])
            item = next((item for item in items if item["id"] == media_id), None)
            if item is None:
                raise HTTPException(404, "File not found.")
            return FileResponse(media_path(session, item))

    @app.delete("/api/sessions/{session_id}/media/{media_id}")
    def remove(session_id: str, media_id: str):
        session = get_session(session_id)
        with session.lock:
            session.available()
            items = session.images + ([session.audio] if session.audio else [])
            item = next((item for item in items if item["id"] == media_id), None)
            if item is None:
                raise HTTPException(404, "File not found.")
            media_path(session, item).unlink(missing_ok=True)
            if item["kind"] == "image":
                session.images.remove(item)
            else:
                session.audio = None
        return session.snapshot()

    def worker(session, options, run_config, run_id):
        work = session.run_directory(run_id)
        output = work / "video.mp4"
        try:
            work.mkdir(parents=True, exist_ok=True)
            audio = media_path(session, session.audio)
            duration = audio_duration(audio)
            session.event("Voice checked", 3)
            check_cancel(session.stop)
            session.event("Transcribing voice with Whisper", 5)
            transcript = Transcriber(config["whisper"]).transcribe(
                audio, duration, session.stop, work
            )
            check_cancel(session.stop)
            (work / "transcript.json").write_text(json.dumps(asdict(transcript), indent=2))
            words = transcript.words
            session.event(f"Voice transcribed · {len(words)} words", 8)
            footage = config["footage"]
            plan = build_plan(
                sentences_from_words(words, duration),
                [Source(str(media_path(session, item)), "image") for item in session.images],
                duration,
                threshold=footage["minimum_words_for_new_clip"],
                seed=options.seed,
                avoid_repeat=footage["avoid_immediate_repetition"],
            )
            session.event(f"Timeline ready · {len(plan.footage)} image sections", 10)
            session.event("Building video", 10)

            def progress(value):
                with session.lock:
                    session.run["progress"] = round(10 + value * 0.9, 1)

            render(plan, audio, output, work, run_config, session.stop, progress)
            session.event("Video passed the builder checks", 100)
            with session.lock:
                session.run.update(
                    status="passed",
                    video=True,
                    duration=duration,
                    bytes=output.stat().st_size,
                    composition=json.loads((work / "composition.json").read_text()),
                )
        except Cancelled:
            session.event("Test stopped")
            with session.lock:
                session.run["status"] = "stopped"
        except Exception as exc:
            log.exception("Video test failed")
            session.event("Test failed")
            with session.lock:
                session.run.update(status="failed", error=str(exc))
        finally:
            with session.lock:
                session.run["finished"] = time.time()
                report = copy.deepcopy(session.run)
            try:
                work.mkdir(parents=True, exist_ok=True)
                (work / "test-report.json").write_text(json.dumps(report, indent=2))
            except OSError:
                log.exception("Could not save test report")
            render_lock.release()

    @app.post("/api/sessions/{session_id}/run", status_code=202)
    def start(session_id: str, options: RunOptions):
        session = get_session(session_id)
        with session.lock:
            session.available()
            if not 3 <= len(session.images) <= 10 or not session.audio:
                raise HTTPException(
                    400, "Add 3–10 images and one voice recording before running a test."
                )
            try:
                check_transcription_config(config)
                check_render_config(config)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            if not info()["ffmpeg"] or not info()["font"]:
                raise HTTPException(
                    400, "FFmpeg, FFprobe and the configured caption font are required."
                )
            if not render_lock.acquire(blocking=False):
                raise HTTPException(409, "Another test is running. Wait for it to finish.")
            run_config = copy.deepcopy(config)
            # The test seed reproduces both image selection and the music choice.
            run_config["footage"]["random_seed"] = options.seed
            if options.size == "preview":
                scale = 360 / config["video"]["width"]
                height = max(2, round(config["video"]["height"] * scale / 2) * 2)
                run_config["video"].update(width=360, height=height, preset="ultrafast")
                for field in ("font_size", "safe_margin", "outline_width"):
                    run_config["captions"][field] = max(1, round(config["captions"][field] * scale))
            run_id = uuid.uuid4().hex
            session.stop.clear()
            session.run = {
                "id": run_id,
                "status": "running",
                "progress": 0,
                "stage": "Starting test",
                "started": time.time(),
                "finished": None,
                "events": [],
                "error": None,
                "video": False,
                "options": options.model_dump(),
                "width": run_config["video"]["width"],
                "height": run_config["video"]["height"],
                "fps": run_config["video"]["fps"],
            }
            session.thread = threading.Thread(
                target=worker, args=(session, options, run_config, run_id), daemon=True
            )
            try:
                session.thread.start()
            except Exception:
                session.run = None
                render_lock.release()
                raise
        return session.snapshot()

    @app.post("/api/sessions/{session_id}/stop")
    def stop(session_id: str):
        session = get_session(session_id)
        with session.lock:
            if session.active:
                session.run["status"] = "stopping"
                session.stop.set()
        return session.snapshot()

    @app.get("/api/sessions/{session_id}/video/{run_id}")
    def video(session_id: str, run_id: str, download: bool = False):
        session = get_session(session_id)
        with session.lock:
            if not session.run or session.run["id"] != run_id or not session.run["video"]:
                raise HTTPException(404, "No completed video for this test.")
            path = session.run_directory(run_id) / "video.mp4"
            return FileResponse(
                path, media_type="video/mp4", filename="video-test.mp4" if download else None
            )

    @app.get("/api/sessions/{session_id}/report")
    def report(session_id: str):
        session = get_session(session_id)
        with session.lock:
            if not session.run:
                raise HTTPException(404, "Run a test first.")
            return JSONResponse(
                copy.deepcopy(session.run),
                headers={"Content-Disposition": 'attachment; filename="test-report.json"'},
            )

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app
