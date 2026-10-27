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
from app.cancellation import Cancelled
from app.config import load_config
from app.dashboard import Dashboard
from app.footage import probe
from app.logger import configure
from app.models import Transcript
from app.processor import Processor
from app.state import RunLock
from app.story import align_lines, numbered_frames, parse_lines, run_story, section_request
from app.timeline import Word
from app.workflows import build_workflow


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


def test_alignment_rejects_lines_with_the_same_start():
    result = align_lines(
        parse_lines("First\nSecond"), [Word("First", 0, 0.2), Word("Second", 0, 0.3)], 1
    )
    assert result[0]["matched"]
    assert not result[1]["matched"]


@pytest.mark.parametrize("cancel_at", ["before", "transcription"])
def test_story_cancellation_never_reaches_renderer(story_section, tmp_path, monkeypatch, cancel_at):
    directory, config = story_section
    request = section_request(directory, tmp_path / "out.mp4", tmp_path / "cancelled-work")
    stop = threading.Event()
    if cancel_at == "before":
        stop.set()

    def transcribe(*args):
        assert cancel_at == "transcription"
        stop.set()
        return Transcript(
            tuple(
                Word(word, i * 0.2, i * 0.2 + 0.1)
                for i, word in enumerate("The red sun The blue moon".split())
            ),
            (),
        )

    monkeypatch.setattr(
        "app.story.render_story", lambda *args: pytest.fail("Do not render after cancellation")
    )
    with pytest.raises(Cancelled):
        run_story(
            request, config, stop, lambda **values: None, SimpleNamespace(transcribe=transcribe)
        )
    assert not request.output.exists()
    assert request.audio.exists() and all(p.exists() for p in request.frames.values())


def test_story_lock_rejects_concurrency_and_allows_repeat(story_section, monkeypatch):
    directory, config = story_section
    rendered = []
    monkeypatch.setattr(
        "app.story.run_story", lambda request, *args, **kwargs: rendered.append(request)
    )
    workflow = build_workflow(config, "story", str(directory))
    executor = workflow.executor(threading.Event(), lambda **values: None)
    lock = RunLock(f"story:{directory.resolve()}")
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="already being rendered"):
            executor.run(None, None, [])
        assert not rendered
    finally:
        lock.release()
    assert executor.run(None, None, [])
    assert executor.run(None, None, [])
    assert rendered[0].output != rendered[1].output
    assert rendered[0].work != rendered[1].work
    assert lock.acquire()
    lock.release()


def test_failed_final_validation_leaves_no_published_or_partial_video(
    story_section, tmp_path, monkeypatch
):
    directory, config = story_section
    request = section_request(directory, tmp_path / "out.mp4", tmp_path / "failed-output")

    def reject(*args):
        raise ValueError("Output validation failed")

    monkeypatch.setattr("app.story.validate_output", reject)
    with pytest.raises(ValueError, match="Output validation failed"):
        run_story(request, config, threading.Event(), lambda **values: None)
    assert not request.output.exists()
    assert not request.output.with_suffix(".partial.mp4").exists()
    assert (request.work / "alignment.json").exists()
    assert request.audio.exists() and all(p.exists() for p in request.frames.values())


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
    # Story must render even when none of poetry's optional dependencies exist.
    c["composition"] = {key: str(directory / "absent" / key) for key in c["composition"]}
    c["captions"]["font_file"] = str(directory / "absent.ttf")
    c["baserow"] = {}
    c["cloudinary"] = {}
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


@pytest.mark.parametrize("line_count", [1, 8])
def test_story_landscape_render_with_fractional_audio_duration(story_section, tmp_path, line_count):
    directory, config = story_section
    duration = 12.013
    config["video"].update(width=320, height=180, fps=24)
    (directory / "script.txt").write_text("\n".join(f"Scene {i}" for i in range(1, line_count + 1)))
    if line_count == 1:
        (directory / "frames" / "2.png").unlink()
    for i in range(3, line_count + 1):
        Image.new("RGB", (120, 160), "red" if i % 2 else "blue").save(
            directory / "frames" / f"{i}.png"
        )
    with wave.open(str(directory / "audio.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * round(duration * 8000))
    words = tuple(
        Word(word, i * 1.5 + j * 0.2, i * 1.5 + j * 0.2 + 0.1)
        for i in range(line_count)
        for j, word in enumerate(f"Scene {i + 1}".split())
    )
    request = section_request(directory, tmp_path / "landscape.mp4", tmp_path / "landscape-work")
    output = run_story(
        request,
        config,
        threading.Event(),
        lambda **values: None,
        SimpleNamespace(transcribe=lambda *args: Transcript(words, ())),
    )
    metadata = probe(output)
    visual = next(s for s in metadata["streams"] if s["codec_type"] == "video")
    assert (visual["width"], visual["height"]) == (320, 180)
    assert int(visual["nb_frames"]) == 289
    plan = json.loads((request.work / "plan.json").read_text())
    assert len(plan["footage"]) == line_count
    assert len([t for t in plan["transitions"] if t["kind"] == "dissolve"]) == line_count - 1
    assert [Path(item["source"]["path"]).stem for item in plan["footage"]] == [
        str(i) for i in range(1, line_count + 1)
    ]


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
