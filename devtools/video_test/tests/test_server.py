"""Exercise buffer writes and real rendering through the developer HTTP API."""

import io
import json
import threading
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.cancellation import check_cancel
from app.config import load_config
from app.footage import probe
from devtools.video_test.server import create_app

HEADERS = {"X-Video-Test": "1"}
TEXT = "Here comes the morning. Light moves through trees. A new day begins."


def image_bytes(color="blue"):
    data = io.BytesIO()
    Image.new("RGB", (160, 200), color).save(data, "PNG")
    return data.getvalue()


def audio_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 32000)
    return data.getvalue()


@pytest.fixture
def client(tmp_path):
    config = load_config()
    for key in config["paths"]:
        config["paths"][key] = str(tmp_path / key)
    config["video"].update(width=180, height=320, fps=20, preset="ultrafast")
    config["captions"].update(font_size=18, safe_margin=12)
    with TestClient(create_app(config), headers=HEADERS) as client:
        client.config = config
        yield client


def session(client):
    response = client.post("/api/sessions")
    assert response.status_code == 201
    return f"/api/sessions/{response.json()['id']}"


def fill(client, base):
    for index, color in enumerate(["red", "green", "blue"]):
        response = client.post(
            f"{base}/media/image?name=image-{index}.png", content=image_bytes(color)
        )
        assert response.status_code == 201, response.text
    response = client.post(f"{base}/media/audio?name=voice.wav", content=audio_bytes())
    assert response.status_code == 201, response.text
    return client.get(base).json()


def wait(client, base):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        state = client.get(base).json()
        if state["run"]["status"] not in {"running", "stopping"}:
            # Wait for diagnostic writes and global render lock release too.
            client.app.state.sessions[state["id"]].thread.join(timeout=5)
            return state
        time.sleep(0.05)
    pytest.fail("Render did not finish")


def test_static_and_local_request_guard(client):
    page = client.get("/")
    assert page.status_code == 200 and "Video Builder Test" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/api/info").json()["preview_size"] == "360 × 640"
    assert client.post("/api/sessions", headers={"X-Video-Test": ""}).status_code == 403
    assert (
        client.post("/api/sessions", headers={"Origin": "https://example.com"}).status_code == 403
    )
    assert client.get("/api/info", headers={"Host": "example.com"}).status_code == 400
    assert client.get("/.env").status_code == 404


def test_uploads_are_buffered_and_isolated(client):
    base, other = session(client), session(client)
    data = fill(client, base)
    image_folder = Path(client.config["paths"]["image_buffer"]) / "ui-test" / data["id"]
    audio_folder = Path(client.config["paths"]["buffer"]) / "ui-test" / data["id"]
    assert len(list(image_folder.glob("*.png"))) == 3
    assert (audio_folder / data["audio"]["file"]).read_bytes() == audio_bytes()
    image_id = data["images"][0]["id"]
    assert client.get(f"{base}/media/{image_id}").status_code == 200
    assert client.get(f"{other}/media/{image_id}").status_code == 404
    response = client.delete(f"{base}/media/{image_id}")
    assert len(response.json()["images"]) == 2
    assert len(list(image_folder.glob("*.png"))) == 2
    assert client.get(other).json()["images"] == []
    previous_audio = audio_folder / data["audio"]["file"]
    response = client.post(f"{base}/media/audio?name=../../new.wav", content=audio_bytes())
    assert response.json()["audio"]["name"] == "new.wav"
    assert not previous_audio.exists()
    assert len(list(audio_folder.glob("*.wav"))) == 1


def test_invalid_uploads_and_image_count(client, monkeypatch):
    base = session(client)
    for kind in ["image", "audio"]:
        assert client.post(f"{base}/media/{kind}", content=b"invalid media").status_code == 400
        assert client.post(f"{base}/media/{kind}", content=b"").status_code == 400
    assert client.post(f"{base}/run", json={"text": TEXT}).status_code == 400
    for _ in range(10):
        assert client.post(f"{base}/media/image", content=image_bytes()).status_code == 201
    assert client.post(f"{base}/media/image", content=image_bytes()).status_code == 400
    assert not list(Path(client.config["paths"]["image_buffer"]).rglob("*.upload"))
    assert not client.get(base).json()["uploading"]
    other = session(client)
    monkeypatch.setattr("devtools.video_test.server.IMAGE_LIMIT", 10)
    assert client.post(f"{other}/media/image", content=image_bytes()).status_code == 413
    assert client.get(other).json()["images"] == []


def test_real_render_from_buffer_and_video_download(client):
    base = session(client)
    original = fill(client, base)
    response = client.post(f"{base}/run", json={"size": "full", "text": TEXT, "seed": 5})
    assert response.status_code == 202, response.text
    state = wait(client, base)
    run = state["run"]
    assert run["status"] == "passed", run
    assert run["progress"] == 100 and run["duration"] == 2
    video_url = f"{base}/video/{run['id']}"
    response = client.get(video_url)
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert client.get(video_url, headers={"Range": "bytes=0-31"}).status_code == 206
    assert (
        'filename="video-test.mp4"'
        in client.get(video_url + "?download=true").headers["content-disposition"]
    )
    output = Path(client.config["paths"]["output"]) / "ui-test" / state["id"] / f"{run['id']}.mp4"
    details = probe(output)
    streams = {item["codec_type"]: item for item in details["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert (streams["video"]["width"], streams["video"]["height"]) == (180, 320)
    assert abs(float(details["format"]["duration"]) - 2) < 0.05
    work = Path(client.config["paths"]["temp"]) / "ui-test" / state["id"] / run["id"]
    plan = json.loads((work / "plan.json").read_text())
    assert len(plan["footage"]) == 3 and len(plan["captions"]) == 3
    assert all("/ui-test/" in item["source"]["path"] for item in plan["footage"])
    assert json.loads((work / "test-report.json").read_text())["status"] == "passed"
    assert client.get(f"{base}/report").json()["status"] == "passed"
    assert client.get(base).json()["images"] == original["images"]
    # A second actual run uses scaled preview settings and leaves the first output intact.
    assert client.post(f"{base}/run", json={"size": "preview", "text": TEXT}).status_code == 202
    second = wait(client, base)["run"]
    assert second["status"] == "passed" and (second["width"], second["height"]) == (360, 640)
    assert output.is_file()


def test_running_inputs_are_frozen_and_stop_releases_worker(client, monkeypatch):
    started = threading.Event()

    def slow_render(plan, audio, output, work, config, stop, progress):
        started.set()
        assert stop.wait(8), "Test must cancel the worker"
        check_cancel(stop)

    monkeypatch.setattr("devtools.video_test.server.render", slow_render)
    base, other = session(client), session(client)
    state = fill(client, base)
    fill(client, other)
    assert client.post(f"{base}/run", json={"text": TEXT}).status_code == 202
    assert started.wait(3)
    assert client.post(f"{base}/run", json={"text": TEXT}).status_code == 409
    assert client.post(f"{other}/run", json={"text": TEXT}).status_code == 409
    assert client.delete(f"{base}/media/{state['images'][0]['id']}").status_code == 409
    assert client.post(f"{base}/media/audio", content=audio_bytes()).status_code == 409
    assert client.post(f"{base}/stop").status_code == 200
    stopped = wait(client, base)
    assert stopped["run"]["status"] == "stopped" and not stopped["run"]["video"]
    assert client.delete(f"{base}/media/{state['images'][0]['id']}").status_code == 200


def test_invalid_settings_and_render_failure_are_visible(client, monkeypatch):
    base = session(client)
    fill(client, base)
    assert client.post(f"{base}/run", json={"text": ""}).status_code == 400
    assert client.post(f"{base}/run", json={"text": TEXT, "size": "huge"}).status_code == 422
    monkeypatch.delenv(client.config["whisper"]["api_key_env"], raising=False)
    assert client.post(f"{base}/run", json={"captions": "speech"}).status_code == 400

    def fail(*args, **kwargs):
        raise RuntimeError("Test encoder failure")

    monkeypatch.setattr("devtools.video_test.server.render", fail)
    for _ in range(2):
        assert client.post(f"{base}/run", json={"text": TEXT}).status_code == 202
        run = wait(client, base)["run"]
        assert run["status"] == "failed" and run["error"] == "Test encoder failure"
        assert client.get(f"{base}/video/{run['id']}").status_code == 404
