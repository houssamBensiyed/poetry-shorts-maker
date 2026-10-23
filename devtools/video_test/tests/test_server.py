"""Exercise temporary session uploads and real rendering through the developer API."""

import io
import json
import subprocess
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.cancellation import check_cancel
from app.config import load_config
from app.footage import probe
from app.transcription import normalize_response
from devtools.video_test.server import create_app

HEADERS = {"X-Video-Test": "1"}
PAYLOAD = {
    "text": "First test sentence. Another image appears. Last words here.",
    "words": [
        {"word": text, "start": start, "end": start + 0.08}
        for text, start in zip(
            "First test sentence Another image appears Last words here".split(),
            [0, 0.15, 0.36, 0.8, 0.96, 1.1, 1.5, 1.65, 1.81],
            strict=True,
        )
    ],
    "segments": [
        {
            "text": "First test sentence. Another image appears. Last words here.",
            "start": 0,
            "end": 1.89,
        }
    ],
}


def image_bytes(color="blue"):
    data = io.BytesIO()
    Image.new("RGB", (160, 200), color).save(data, "PNG")
    return data.getvalue()


def audio_bytes():
    data = io.BytesIO()
    clock = np.arange(32000) / 16000
    envelope = 0.5 + 0.4 * np.sin(2 * np.pi * 3 * clock)
    signal = envelope * (
        0.3 * np.sin(2 * np.pi * 220 * clock) + 0.15 * np.sin(2 * np.pi * 617 * clock)
    )
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((signal * 32767).astype("<i2").tobytes())
    return data.getvalue()


def assert_voice_and_music_mixed(source, output, music):
    """Compare decoded samples against the requested voice/music mix, not just metadata."""

    def decode(path, duration=None):
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                *(["-t", str(duration)] if duration is not None else []),
                "-map",
                "0:a:0",
                "-af",
                "pan=mono|c0=c0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        return np.frombuffer(result.stdout, dtype="<f4")

    original, rendered = decode(source), decode(output)
    assert abs(len(original) - len(rendered)) <= 1024
    length = min(len(original), len(rendered))
    original, rendered = original[:length], rendered[:length]
    soundtrack = decode(music, duration=length / 16000)[:length]
    voice_gain, music_gain = 10 ** (3.4 / 20), 10 ** (-12.0 / 20)
    expected = original * voice_gain + soundtrack * music_gain
    assert np.sqrt(np.mean(original**2)) > 0.05, "The input must contain audible sound"
    assert np.corrcoef(expected, rendered)[0, 1] > 0.98, "The output must carry the requested mix"
    error = np.sqrt(np.mean((rendered - expected) ** 2)) / np.sqrt(np.mean(expected**2))
    assert error < 0.06, "AAC output must preserve the requested voice/music gains"
    recovered_gain = np.dot(original, rendered - soundtrack * music_gain) / np.dot(
        original, original
    )
    assert recovered_gain == pytest.approx(voice_gain, rel=0.05)


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = load_config()
    for key in config["paths"]:
        config["paths"][key] = str(tmp_path / key)
    config["video"].update(width=180, height=320, fps=20, preset="ultrafast")
    config["captions"].update(font_size=18, safe_margin=12)
    config["whisper"].update(
        api_url="https://whisper.example/v1/audio/transcriptions",
        model="whisper-test",
        api_key_env="VIDEO_TEST_WHISPER_KEY",
    )
    # Every operation, including full renders and failed uploads, must preserve
    # production inputs and output directories exactly as they were.
    production = {}
    for key, name, content in [
        ("buffer", "voice-000001.wav", audio_bytes()),
        ("image_buffer", "production.png", image_bytes()),
        ("output", "production.mp4", b"existing production video"),
    ]:
        directory = Path(config["paths"][key])
        directory.mkdir()
        (directory / name).write_bytes(content)
        production[key] = {name: content}
    monkeypatch.setenv("VIDEO_TEST_WHISPER_KEY", "test-only")
    requests = []
    response = {"status": 200, "body": PAYLOAD}

    def handle(request):
        requests.append(request)
        assert str(request.url) == config["whisper"]["api_url"]
        assert request.headers["authorization"] == "Bearer test-only"
        body = request.read()
        assert b"whisper-test" in body and b"verbose_json" in body
        assert b"word" in body and b"segment" in body
        assert b"fLaC" in body, "The core transcriber must prepare actual FLAC audio"
        return httpx.Response(response["status"], json=response["body"])

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.transcription.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with TestClient(create_app(config), headers=HEADERS) as client:
        client.whisper_requests = requests
        client.whisper_response = response
        client.config = config
        yield client
    for key, expected in production.items():
        directory = Path(config["paths"][key])
        actual = {
            str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*")
            if p.is_file()
        }
        assert actual == expected, f"Dev tool changed the production {key} directory"
        assert {str(p.relative_to(directory)) for p in directory.rglob("*")} == set(expected), (
            f"Dev tool created extra directories in the production {key} folder"
        )


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
    assert "temp/ui-test/" in page.text
    assert (
        "buffer/images/ui-test/" not in page.text and "buffer/voiceovers/ui-test/" not in page.text
    )
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


def test_uploads_stay_in_temp_and_sessions_are_isolated(client):
    base, other = session(client), session(client)
    data = fill(client, base)
    temporary = Path(client.config["paths"]["temp"]) / "ui-test" / data["id"]
    image_folder = temporary / "images"
    audio_folder = temporary / "voiceovers"
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


def test_test_uploads_do_not_create_missing_production_directories(client, tmp_path):
    config = {**client.config, "paths": {**client.config["paths"]}}
    production = tmp_path / "unused-production"
    for key in ("buffer", "image_buffer", "output"):
        config["paths"][key] = str(production / key)
    with TestClient(create_app(config), headers=HEADERS) as isolated:
        fill(isolated, session(isolated))
    assert not production.exists(), "UI uploads must only create temporary session directories"


def test_invalid_uploads_and_image_count(client, monkeypatch):
    base = session(client)
    for kind in ["image", "audio"]:
        assert client.post(f"{base}/media/{kind}", content=b"invalid media").status_code == 400
        assert client.post(f"{base}/media/{kind}", content=b"").status_code == 400
    assert client.post(f"{base}/run", json={}).status_code == 400
    for _ in range(10):
        assert client.post(f"{base}/media/image", content=image_bytes()).status_code == 201
    assert client.post(f"{base}/media/image", content=image_bytes()).status_code == 400
    assert not list(Path(client.config["paths"]["temp"]).rglob("*.upload"))
    assert not client.get(base).json()["uploading"]
    other = session(client)
    monkeypatch.setattr("devtools.video_test.server.IMAGE_LIMIT", 10)
    assert client.post(f"{other}/media/image", content=image_bytes()).status_code == 413
    assert client.get(other).json()["images"] == []


def test_real_render_from_buffer_and_video_download(client):
    base = session(client)
    original = fill(client, base)
    response = client.post(f"{base}/run", json={"size": "full", "seed": 5})
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
    temporary = Path(client.config["paths"]["temp"]) / "ui-test" / state["id"]
    output = temporary / "runs" / run["id"] / "video.mp4"
    details = probe(output)
    streams = {item["codec_type"]: item for item in details["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["audio"]["codec_name"] == "aac"
    assert (streams["video"]["width"], streams["video"]["height"]) == (180, 320)
    assert abs(float(details["format"]["duration"]) - 2) < 0.05
    voice = temporary / "voiceovers" / original["audio"]["file"]
    composition = run["composition"]
    assert composition["voice_gain_db"] == 3.4 and composition["music_gain_db"] == -12.0
    assert composition["blend_mode"] == "difference"
    assert_voice_and_music_mixed(voice, output, composition["music"])
    work = temporary / "runs" / run["id"]
    plan = json.loads((work / "plan.json").read_text())
    assert len(plan["footage"]) == 3 and len(plan["captions"]) == 3
    words = [word for caption in plan["captions"] for word in caption["sentence"]["words"]]
    assert [word["text"] for word in words] == [
        word.text for word in normalize_response(PAYLOAD, 2).words
    ]
    assert [word["start"] for word in words] == [word["start"] for word in PAYLOAD["words"]]
    assert json.loads((work / "transcript.json").read_text())["words"] == words
    assert len(client.whisper_requests) == 1
    messages = [event["message"] for event in run["events"]]
    assert messages.index("Transcribing voice with Whisper") < messages.index("Building video")
    assert all("/ui-test/" in item["source"]["path"] for item in plan["footage"])
    assert json.loads((work / "test-report.json").read_text())["status"] == "passed"
    assert client.get(f"{base}/report").json()["status"] == "passed"
    assert client.get(base).json()["images"] == original["images"]
    # A second actual run uses scaled preview settings and leaves the first output intact.
    assert client.post(f"{base}/run", json={"size": "preview"}).status_code == 202
    second = wait(client, base)["run"]
    assert second["status"] == "passed" and (second["width"], second["height"]) == (360, 640)
    second_output = temporary / "runs" / second["id"] / "video.mp4"
    assert_voice_and_music_mixed(voice, second_output, second["composition"]["music"])
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
    assert client.post(f"{base}/run", json={}).status_code == 202
    assert started.wait(3)
    assert client.post(f"{base}/run", json={}).status_code == 409
    assert client.post(f"{other}/run", json={}).status_code == 409
    assert client.delete(f"{base}/media/{state['images'][0]['id']}").status_code == 409
    assert client.post(f"{base}/media/audio", content=audio_bytes()).status_code == 409
    assert client.post(f"{base}/stop").status_code == 200
    stopped = wait(client, base)
    assert stopped["run"]["status"] == "stopped" and not stopped["run"]["video"]
    assert client.delete(f"{base}/media/{state['images'][0]['id']}").status_code == 200


def test_invalid_settings_and_render_failure_are_visible(client, monkeypatch):
    base = session(client)
    fill(client, base)
    assert (
        client.post(f"{base}/run", json={"captions": "sample", "text": "Fake"}).status_code == 422
    )
    assert client.post(f"{base}/run", json={"size": "huge"}).status_code == 422

    def fail(*args, **kwargs):
        raise RuntimeError("Test encoder failure")

    monkeypatch.setattr("devtools.video_test.server.render", fail)
    for _ in range(2):
        assert client.post(f"{base}/run", json={}).status_code == 202
        run = wait(client, base)["run"]
        assert run["status"] == "failed" and run["error"] == "Test encoder failure"
        assert client.get(f"{base}/video/{run['id']}").status_code == 404


@pytest.mark.parametrize("missing", ["key", "api_url", "model"])
def test_missing_whisper_config_blocks_run(client, monkeypatch, missing):
    config = client.config.copy()
    config["whisper"] = config["whisper"].copy()
    if missing == "key":
        monkeypatch.delenv("VIDEO_TEST_WHISPER_KEY")
    else:
        config["whisper"][missing] = ""
    with TestClient(create_app(config), headers=HEADERS) as unconfigured:
        base = session(unconfigured)
        fill(unconfigured, base)
        info = unconfigured.get("/api/info").json()
        assert not info["speech"] and info["speech_error"]
        assert unconfigured.post(f"{base}/run", json={}).status_code == 400
        assert unconfigured.get(base).json()["run"] is None
    assert not client.whisper_requests


def test_transcription_failure_does_not_render_or_substitute_text(client, monkeypatch):
    base = session(client)
    fill(client, base)

    client.whisper_response["status"] = 401

    def no_render(*args):
        pytest.fail("Rendering must wait for successful transcription")

    monkeypatch.setattr("devtools.video_test.server.render", no_render)
    assert client.post(f"{base}/run", json={}).status_code == 202
    run = wait(client, base)["run"]
    assert run["status"] == "failed" and "HTTP 401" in run["error"]
    assert not run["video"]
    assert "Building video" not in [event["message"] for event in run["events"]]


@pytest.mark.parametrize("missing", ["music_directory", "overlay_file"])
def test_missing_composition_assets_block_before_transcription(client, tmp_path, missing):
    config = client.config.copy()
    config["composition"] = {**config["composition"], missing: str(tmp_path / "missing")}
    with TestClient(create_app(config), headers=HEADERS) as unconfigured:
        base = session(unconfigured)
        fill(unconfigured, base)
        info = unconfigured.get("/api/info").json()
        assert not info["assets"] and info["assets_error"]
        response = unconfigured.post(f"{base}/run", json={})
        assert response.status_code == 400 and info["assets_error"] in response.json()["detail"]
        assert unconfigured.get(base).json()["run"] is None
    assert not client.whisper_requests


def test_stop_during_transcription_prevents_render(client, monkeypatch):
    base = session(client)
    fill(client, base)
    started = threading.Event()

    def transcribe(self, audio, duration, stop, work):
        started.set()
        assert stop.wait(5)
        check_cancel(stop)

    monkeypatch.setattr("app.transcription.Transcriber.transcribe", transcribe)
    assert client.post(f"{base}/run", json={}).status_code == 202
    assert started.wait(3)
    assert client.post(f"{base}/stop").status_code == 200
    run = wait(client, base)["run"]
    assert run["status"] == "stopped" and not run["video"]
    assert "Building video" not in [event["message"] for event in run["events"]]
