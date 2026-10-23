"""Verify local key loading without shell execution or secret disclosure."""

import os

import pytest

from devtools.video_test.environment import load_whisper_key

CONFIG = {"whisper": {"api_key_env": "VIDEO_TEST_WHISPER_KEY"}}


@pytest.mark.parametrize(
    "line, expected",
    [
        ("VIDEO_TEST_WHISPER_KEY=test-value", "test-value"),
        ('export VIDEO_TEST_WHISPER_KEY="test value" # comment', "test value"),
        ("VIDEO_TEST_WHISPER_KEY='$(do-not-run)'", "$(do-not-run)"),
        ('VIDEO_TEST_WHISPER_KEY="value#with-hash"', "value#with-hash"),
    ],
)
def test_loads_only_configured_key(tmp_path, monkeypatch, line, expected):
    monkeypatch.delenv("VIDEO_TEST_WHISPER_KEY", raising=False)
    monkeypatch.delenv("VIDEO_TEST_OTHER_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("VIDEO_TEST_OTHER_KEY=ignored\n" + line + "\n")
    load_whisper_key(CONFIG, path)
    assert os.environ["VIDEO_TEST_WHISPER_KEY"] == expected
    assert "VIDEO_TEST_OTHER_KEY" not in os.environ


def test_exported_key_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_TEST_WHISPER_KEY", "exported")
    path = tmp_path / ".env"
    path.write_text("VIDEO_TEST_WHISPER_KEY=file-value")
    load_whisper_key(CONFIG, path)
    assert os.environ["VIDEO_TEST_WHISPER_KEY"] == "exported"


def test_missing_and_invalid_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_TEST_WHISPER_KEY", raising=False)
    path = tmp_path / ".env"
    load_whisper_key(CONFIG, path)
    assert "VIDEO_TEST_WHISPER_KEY" not in os.environ
    path.write_text('VIDEO_TEST_WHISPER_KEY="private-value')
    with pytest.raises(ValueError) as error:
        load_whisper_key(CONFIG, path)
    assert "private-value" not in str(error.value)
