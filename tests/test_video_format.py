"""Verify output choices and atomic project setting persistence."""

from copy import deepcopy

import pytest

from app.config import load_config
from app.settings import save_shake_settings
from app.video_format import with_video_format


@pytest.mark.parametrize(
    "ratio,full,preview",
    [
        ("9:16", (1080, 1920), (360, 640)),
        ("16:9", (1920, 1080), (640, 360)),
        ("1:1", (1080, 1080), (360, 360)),
        ("4:5", (1080, 1350), (360, 450)),
    ],
)
def test_format_and_preview_are_isolated(ratio, full, preview):
    original = load_config()
    before = deepcopy(original)
    for small, dimensions in [(False, full), (True, preview)]:
        chosen = with_video_format(original, ratio, preview=small)
        assert (chosen["video"]["width"], chosen["video"]["height"]) == dimensions
        assert chosen["captions"]["font_size"] == (23 if small else 70)
    assert original == before
    assert with_video_format(original) == original
    with pytest.raises(ValueError, match="Aspect ratio"):
        with_video_format(original, "invalid")


@pytest.mark.parametrize("section", ["", "shake: {zoom: 1.03}\n", "shake:\n  zoom: 1.03\n"])
def test_apply_preserves_unrelated_project_settings(tmp_path, section):
    path = tmp_path / "config.yaml"
    before = (
        "# Project preferences\nvideo:\n  width: 1920\n  height: 1080\n"
        + section
        + "# Other settings\npaths:\n  buffer: local-voices\n"
    )
    path.write_text(before)
    settings = load_config(path)["shake"] | {"period_x": 1.2, "amplitude_y": 0.2}
    save_shake_settings(path, settings)
    result = load_config(path)
    assert result["shake"] == settings
    assert result["video"]["width"] == 1920
    assert result["paths"]["buffer"] == str(tmp_path / "local-voices")
    assert path.read_text().startswith(
        "# Project preferences\nvideo:\n  width: 1920\n  height: 1080\n"
    )
    assert "# Other settings\npaths:\n  buffer: local-voices\n" in path.read_text()
    assert not list(tmp_path.glob(".shake-*"))


def test_failed_apply_keeps_original_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("video: {width: 1920, height: 1080}\n")
    original = path.read_bytes()

    def fail(*args):
        raise OSError("Read-only project")

    monkeypatch.setattr(type(path), "replace", fail)
    with pytest.raises(OSError, match="Read-only"):
        save_shake_settings(path, load_config()["shake"])
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".shake-*"))
