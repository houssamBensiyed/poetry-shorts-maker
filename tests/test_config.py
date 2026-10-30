import pytest

from app.config import load_config


@pytest.mark.parametrize(
    "text",
    [
        "video:\n  fps: 0",
        "video:\n  width: 321",
        "captions:\n  persist_during_silence: false",
        "captions:\n  color: red",
        "baserow:\n  api_url: null",
        "baserow:\n  table_id: invalid",
        "whisper:\n  word_timestamps: false",
        "unknown: {}",
        "video: [broken",
        "composition:\n  images_directory: null",
        "composition:\n  videos_directory: null",
        "footage:\n  mode: invalid",
        "shake:\n  amplitude_x: 1.1",
        "shake:\n  zoom: .99",
        "shake:\n  period_x: 0",
        "shake:\n  enabled: 1",
        "shake:\n  period_y: .nan",
        "captions:\n  channel_name: 5",
        "composition:\n  images_directory: ''",
        "composition:\n  music_directory: null",
        "composition:\n  overlay_file: ''",
    ],
)
def test_invalid_configuration_has_clear_error(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


def test_relative_paths_and_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}")
    c = load_config(path)
    assert c["composition"]["images_directory"] == str(tmp_path / "assets" / "images")
    assert c["composition"]["videos_directory"] == str(tmp_path / "assets" / "videos")
    assert c["footage"]["mode"] == "image"
    assert "image_buffer" not in c["paths"]
    assert c["video"]["fps"] == 30
    assert c["paths"]["output"] == str(tmp_path / "output")
    assert c["whisper"]["api_key_env"] == "WHISPER_API_KEY"
    assert c["whisper"]["api_url"] == ""
    assert c["composition"]["music_directory"] == str(tmp_path / "assets" / "music")


@pytest.mark.parametrize(
    "text",
    [
        "false",
        "0",
        "[]",
        '""',
        "video:\n  preset: []",
        "captions:\n  position: {}",
        "captions:\n  outline_width: true",
        "whisper:\n  timeout_seconds: true",
    ],
)
def test_invalid_yaml_shapes_and_boolean_numbers_are_rejected(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)
