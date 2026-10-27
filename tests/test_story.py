import asyncio
import json
import subprocess
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from textual.widgets import Input, Select

from api import create_app
from app.config import load_config
from app.dashboard import Dashboard
from app.logger import configure
from app.models import Transcript
from app.processor import Processor
from app.story import align_lines, numbered_frames, parse_lines, run_story, section_request
from app.timeline import Word


@pytest.fixture
def story_section(tmp_path, monkeypatch):
    directory = tmp_path / "section"
    directory.mkdir()
    (directory / "script.txt").write_text("The red sun.\nThe blue moon.")
    (directory / "frames").mkdir()
    for i, color in [(1, "red"), (2, "blue")]:
        Image.new("RGB", (120, 160), color).save(directory / "frames" / f"{i}.png")
    with wave.open(str(directory / "audio.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 16000)
    transcript = Transcript(
        tuple(
            Word(word, start + j * 0.1, start + j * 0.1 + 0.08)
            for start, text in [(0.1, "The red sun."), (1.2, "The blue moon.")]
            for j, word in enumerate(text.split())
        ),
        (),
    )
    monkeypatch.setenv("WHISPER_API_KEY", "test")
    monkeypatch.setattr(
        "app.story.Transcriber", lambda config: SimpleNamespace(transcribe=lambda *args: transcript)
    )
    c = load_config()
    c["video"].update(width=160, height=240, fps=20, preset="ultrafast")
    c["captions"].update(font_size=16, safe_margin=10)
    for key in c["paths"]:
        c["paths"][key] = str(tmp_path / key)
    return directory, c


def test_parser_and_uncertain_alignment():
    lines = parse_lines("\nFirst line.\n\nSecond line.\n")
    assert [line["index"] for line in lines] == [1, 2]
    result = align_lines(lines, [Word("Second", 1, 1.2), Word("line", 1.3, 1.5)], 2)
    assert not result[0]["matched"] and result[1]["matched"]
    repeated = parse_lines("Go home.\nGo home.")
    words = [Word(word, i * 0.2, i * 0.2 + 0.1) for i, word in enumerate("Go home Go home".split())]
    result = align_lines(repeated, words, 2)
    assert [line["start"] for line in result] == [0, 0.4]
    with pytest.raises(ValueError, match="Duplicate"):
        numbered_frames([Path("1.jpg"), Path("1.png")])


def test_failed_alignment_preserves_diagnostics_and_inputs(story_section, tmp_path, monkeypatch):
    directory, config = story_section
    request = section_request(directory, tmp_path / "out.mp4", tmp_path / "failed-work")
    transcriber = SimpleNamespace(
        transcribe=lambda *args: Transcript((Word("Unrelated", 0, 0.5),), ())
    )
    monkeypatch.setattr(
        "app.story.render_story", lambda *args: pytest.fail("Do not render uncertain alignment")
    )
    with pytest.raises(ValueError, match="Uncertain alignment"):
        run_story(request, config, threading.Event(), lambda **values: None, transcriber)
    assert (request.work / "lines.json").exists()
    assert (request.work / "alignment.json").exists()
    assert not request.output.exists()
    assert request.audio.exists() and all(p.exists() for p in request.frames.values())


def test_missing_frame_rejected_before_transcription(story_section, tmp_path):
    directory, config = story_section
    request = section_request(directory, tmp_path / "out.mp4", tmp_path / "missing-work")
    request.frames.pop(2)
    with pytest.raises(ValueError, match="missing \\[2\\]"):
        run_story(
            request,
            config,
            threading.Event(),
            lambda **values: None,
            SimpleNamespace(transcribe=lambda *args: pytest.fail("Validate inputs first")),
        )
    assert not request.work.exists()


def test_story_real_render_fades_dissolve_duration_and_retention(story_section, tmp_path):
    directory, c = story_section
    request = section_request(directory, tmp_path / "out.mp4", tmp_path / "work")
    output = run_story(request, c, threading.Event(), lambda **values: None)
    plan = json.loads((request.work / "plan.json").read_text())
    assert plan["duration"] == 2 and plan["footage"][0]["end"] == 1.2
    assert (directory / "audio.wav").exists() and len(list((directory / "frames").iterdir())) == 2
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(output),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 240, 160, 3)
    assert len(frames) == 40
    assert frames[0].max() < 5 and frames[-1].max() < 5
    assert frames[12, 120, 80, 0] > 240
    assert frames[22, 120, 80, 0] > 20 and frames[22, 120, 80, 2] > 20
    assert frames[25, 120, 80, 2] > 240


def test_story_api_routes_without_touching_voice_queue(story_section, monkeypatch):
    directory, c = story_section
    monkeypatch.setenv("VIDEO_BUILDER_API_KEY", "test")
    processor = Processor(c)
    with TestClient(create_app(c, processor)) as client:
        headers = {"X-API-Key": "test"}
        assert client.get("/run?style=story", headers=headers).status_code == 422
        assert (
            client.get(
                "/run", params={"style": "story", "section": str(directory)}, headers=headers
            ).status_code
            == 202
        )
        processor.thread.join(timeout=20)
        state = client.get("/status", headers=headers).json()
        assert state["completed"] == 1, state
        assert Path(state["output"]).is_file()
        assert not Path(c["paths"]["buffer"]).exists()


def test_story_console(story_section, tmp_path):
    directory, c = story_section

    async def scenario():
        processor = Processor(c)
        try:
            app = Dashboard(processor, configure(tmp_path))
            async with app.run_test(size=(100, 40)) as pilot:
                app.query_one("#style", Select).value = "story"
                app.query_one("#section", Input).value = str(directory)
                await pilot.pause(0.3)
                await pilot.click("#start")
                await asyncio.to_thread(processor.thread.join, 20)
                assert processor.status()["completed"] == 1, processor.status()
                await pilot.click("#exit")
        finally:
            processor.close()

    asyncio.run(scenario())
