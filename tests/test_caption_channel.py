import re
import subprocess

import numpy as np
import pytest

from app.captions import channel_metrics, write_ass
from app.config import load_config
from app.timeline import Source, Word, build_plan, sentences_from_words


def caption_plan():
    words = [
        Word(word, 0.2 + i * 0.2, 0.3 + i * 0.2)
        for i, word in enumerate("These words form two lines.".split())
    ]
    return build_plan(sentences_from_words(words, 2), [Source("unused", "image")], 2)


@pytest.mark.parametrize("position", ["top", "center", "bottom"])
def test_optional_channel_is_small_persistent_and_below_multiline_caption(tmp_path, position):
    c = load_config()
    c["captions"].update(position=position, max_words_per_line=3, channel_name="  My Channel  ")
    path = tmp_path / "captions.ass"
    write_ass(caption_plan(), c["captions"], c["video"], path)
    events = [line for line in path.read_text().splitlines() if line.startswith("Dialogue:")]
    labels = [line for line in events if ",Channel," in line]
    assert len(labels) == 1
    assert "0:00:00.20,0:00:02.00" in labels[0] and labels[0].endswith("My Channel")
    font_size = float(re.search(r"\\fs([0-9.]+)", labels[0])[1])
    assert font_size == pytest.approx(8 * 96 / 72)
    label_y = float(re.search(r"\\pos\([^,]+,([0-9.]+)\)", labels[0])[1])
    text_y = float(re.search(r"\\pos\([^,]+,([0-9.]+)\)", events[1])[1])
    assert text_y < label_y < c["video"]["height"] - c["captions"]["safe_margin"]
    assert all("My Channel" not in event for event in events[1:])
    c["captions"]["channel_name"] = " \t "
    write_ass(caption_plan(), c["captions"], c["video"], path)
    assert ",Channel," not in path.read_text()
    assert channel_metrics(c["captions"], {"width": 360})[0] == pytest.approx(font_size / 3)


def test_channel_is_rendered_below_caption_and_absent_before_speech(tmp_path):
    c = load_config()
    c["captions"].update(channel_name="My Channel", max_words_per_line=3)
    path = tmp_path / "captions.ass"
    write_ass(caption_plan(), c["captions"], c["video"], path)
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=1080x1920:r=10:d=2",
            "-vf",
            f"ass={path}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 1920, 1080, 3)
    assert frames[0].max() == 0
    mask = frames[-1].max(axis=2) > 100
    occupied = np.flatnonzero(mask.any(axis=1))
    groups = np.split(occupied, np.where(np.diff(occupied) > 1)[0] + 1)
    assert len(groups) == 3, "Two caption lines followed by the small channel label"
    assert 2 <= len(groups[-1]) <= 12
    assert len(groups[-1]) < len(groups[-2]) / 2
    assert groups[-1][0] > groups[-2][-1]
