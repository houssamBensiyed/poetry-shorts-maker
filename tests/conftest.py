import io
import subprocess
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image


@pytest.fixture
def video_asset(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=80x120:r=20:d=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=3000:duration=0.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def composition_assets(tmp_path):
    """Short distinct-tone music and a gray overlay with deliberately unwanted audio."""
    music = tmp_path / "music"
    music.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=0.4",
            "-ac",
            "2",
            str(music / "tone.wav"),
        ],
        check=True,
        capture_output=True,
    )
    overlay = tmp_path / "overlay with spaces.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x202020:s=80x120:r=20:d=0.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=2000:sample_rate=48000:duration=0.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(overlay),
        ],
        check=True,
        capture_output=True,
    )
    return {"music_directory": str(music), "overlay_file": str(overlay)}


@pytest.fixture
def file_server():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000)
    audio = buffer.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.end_headers()
                return
            status, content, kind = 200, audio, "application/octet-stream"
            if self.path.startswith("/image"):
                buffer = io.BytesIO()
                Image.new("RGB", (160, 120), "blue").save(buffer, format="PNG")
                content, kind = buffer.getvalue(), "image/png"
            if self.path == "/login":
                content, kind = b"<html>Sign in</html>", "text/html"
            if self.path == "/missing":
                status = 404
            if self.path == "/empty":
                content = b""
            if self.path == "/large":
                content = b"x" * (1024 * 1024 + 1)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", audio
    server.shutdown()
    server.server_close()
    thread.join()
