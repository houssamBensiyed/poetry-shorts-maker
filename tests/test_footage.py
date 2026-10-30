import pytest

from app.config import load_config
from app.footage import LocalFootage, audio_duration, video_duration


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "0", "-1"])
def test_invalid_audio_duration_is_rejected_before_transcription(monkeypatch, duration):
    monkeypatch.setattr(
        "app.footage.probe",
        lambda path: {
            "streams": [{"codec_type": "audio", "duration": duration}],
            "format": {},
        },
    )
    with pytest.raises(ValueError, match="duration"):
        audio_duration("unused.audio")


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "0", "-1"])
def test_invalid_video_duration(monkeypatch, duration):
    monkeypatch.setattr(
        "app.footage.probe",
        lambda path: {"streams": [{"codec_type": "video", "duration": duration}], "format": {}},
    )
    with pytest.raises(ValueError, match="duration"):
        video_duration("clip.mp4")


def test_audio_cover_art_is_not_footage(monkeypatch):
    monkeypatch.setattr(
        "app.footage.probe",
        lambda path: {
            "streams": [
                {"codec_type": "video", "disposition": {"attached_pic": 1}},
                {"codec_type": "audio"},
            ],
            "format": {"duration": "10"},
        },
    )
    with pytest.raises(ValueError, match="no video stream"):
        video_duration("song.mp4")


def test_video_catalog_is_read_only_and_does_not_require_images(tmp_path, monkeypatch):
    c = load_config()
    c["footage"]["mode"] = "video"
    c["composition"]["images_directory"] = str(tmp_path / "absent-images")
    c["composition"]["videos_directory"] = str(tmp_path / "videos")
    with pytest.raises(ValueError, match="composition.videos_directory"):
        LocalFootage(c).sources()
    directory = tmp_path / "videos"
    directory.mkdir()
    (directory / "clip.MP4").write_bytes(b"video")
    (directory / "ignore.txt").write_text("not footage")
    (directory / "nested").mkdir()
    (directory / "nested" / "other.mp4").write_bytes(b"not a root asset")
    monkeypatch.setattr("app.footage.video_duration", lambda path: 2.5)
    sources = LocalFootage(c).sources()
    assert len(sources) == 1 and sources[0].duration == 2.5
    assert sources[0].kind == "video" and sources[0].path == str(directory / "clip.MP4")
    assert (directory / "clip.MP4").read_bytes() == b"video"
    assert not (tmp_path / "absent-images").exists()
