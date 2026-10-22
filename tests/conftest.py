import io
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image


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
