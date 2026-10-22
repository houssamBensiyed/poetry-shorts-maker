import threading

import httpx
import pytest

from app.config import load_config
from app.timeline import sentences_from_words
from app.transcription import Transcriber, normalize_response
from app.video_builder import Cancelled


def payload():
    return {
        "text": "Hello there. Yes!",
        "words": [
            {"word": "Hello", "start": 0, "end": 0.5},
            {"word": "there", "start": 0.5, "end": 1},
            {"word": "Yes", "start": 2, "end": 2.5},
        ],
        "segments": [{"text": "Hello there. Yes!", "start": 0, "end": 2.5}],
    }


def test_restores_sentence_punctuation_without_changing_timestamps():
    result = normalize_response(payload(), 3)
    assert [w.text for w in result.words] == ["Hello", "there.", "Yes!"]
    assert [w.start for w in result.words] == [0, 0.5, 2]
    assert len(sentences_from_words(result.words, 3)) == 2


@pytest.mark.parametrize("overrun", [0.2, 0.28, 0.5])
def test_small_end_overruns_are_clipped_without_changing_other_timestamps(overrun):
    data = payload()
    data["words"][-1]["end"] = 3 + overrun
    data["segments"][-1]["end"] = 3 + overrun

    result = normalize_response(data, 3)

    assert [w.start for w in result.words] == [0, 0.5, 2]
    assert [w.end for w in result.words] == [0.5, 1, 3]
    assert result.segments[-1]["end"] == 3
    assert sentences_from_words(result.words, 3)[-1].words[-1].end == 3
    assert data["words"][-1]["end"] == 3 + overrun
    assert data["segments"][-1]["end"] == 3 + overrun


@pytest.mark.parametrize("collection", ["words", "segments"])
@pytest.mark.parametrize(
    "start,end",
    [
        (2, 3.5001),  # Too large to be a small estimate error.
        (3, 3.2),  # Speech must start inside the recording to be clipped.
        (3.1, 3.2),
        (-0.01, 3.2),
        (2.5, 2.4),
        (2, float("nan")),
        (2, float("inf")),
    ],
)
def test_end_clipping_does_not_hide_invalid_timestamps(collection, start, end):
    data = payload()
    data[collection][-1].update(start=start, end=end)
    with pytest.raises(ValueError):
        normalize_response(data, 3)


def test_end_clipping_does_not_hide_unordered_words():
    data = payload()
    data["words"][1]["end"] = 3.3
    data["words"][2]["end"] = 3.2
    with pytest.raises(ValueError, match="ordered"):
        normalize_response(data, 3)


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.pop("words"),
        lambda p: p.pop("segments"),
        lambda p: p.update(text="Different words."),
        lambda p: p["words"][0].pop("start"),
        lambda p: p["words"][0].update(end=99),
    ],
)
def test_invalid_api_timestamps_rejected(change):
    p = payload()
    change(p)
    with pytest.raises(ValueError):
        normalize_response(p, 3)


def test_api_multipart_and_retry(monkeypatch, tmp_path):
    config = load_config()["whisper"]
    config.update(
        api_url="https://transcription.example/v1/audio/transcriptions", model="hosted-whisper"
    )
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")
    monkeypatch.setattr(
        "app.transcription.run_ffmpeg",
        lambda args, *a: (tmp_path / "transcription.flac").write_bytes(b"flac"),
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == config["api_url"]
        assert request.headers["Authorization"] == "Bearer test-only"
        body = request.read()
        for expected in [
            b"hosted-whisper",
            b"verbose_json",
            b"timestamp_granularities[]",
            b"word",
            b"segment",
            b"voiceover.flac",
        ]:
            assert expected in body
        return httpx.Response(503 if len(requests) == 1 else 200, json=payload())

    factory = httpx.Client
    monkeypatch.setattr(
        "app.transcription.httpx.Client",
        lambda **kw: factory(transport=httpx.MockTransport(handler), **kw),
    )
    result = Transcriber(config).transcribe(tmp_path / "voiceover.audio", 3, threading.Event())
    assert len(result.words) == 3 and len(requests) == 2
    assert not (tmp_path / "transcription.flac").exists()


def test_missing_api_key_is_actionable(monkeypatch, tmp_path):
    config = load_config()["whisper"]
    config.update(api_url="https://transcription.example/transcriptions", model="hosted")
    monkeypatch.delenv("WHISPER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="WHISPER_API_KEY"):
        Transcriber(config).transcribe(tmp_path / "input", 3, threading.Event())


def test_cancel_before_api_call():
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        Transcriber(load_config()["whisper"]).transcribe("unused", 3, stop)


def test_real_audio_preparation_preserves_original(monkeypatch, tmp_path):
    import hashlib
    import subprocess

    from app.footage import audio_duration

    audio = tmp_path / "voiceover.audio"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=3", "-f", "wav", str(audio)],
        check=True,
    )
    original = hashlib.sha256(audio.read_bytes()).digest()
    config = load_config()["whisper"]
    config.update(api_url="https://transcription.example/transcriptions", model="hosted")
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")

    def handler(request):
        assert abs(audio_duration(tmp_path / "transcription.flac") - 3) < 0.01
        return httpx.Response(200, json=payload())

    factory = httpx.Client
    monkeypatch.setattr(
        "app.transcription.httpx.Client",
        lambda **kw: factory(transport=httpx.MockTransport(handler), **kw),
    )
    assert len(Transcriber(config).transcribe(audio, 3, threading.Event()).words) == 3
    assert hashlib.sha256(audio.read_bytes()).digest() == original
    assert not (tmp_path / "transcription.flac").exists()


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        False,
        {"text": "hello", "words": [None]},
        {"text": "hello", "words": [{"word": 3}]},
        {"text": "", "words": False},
        {"text": "", "segments": {}},
        {"text": "", "segments": [None]},
        {"text": "", "segments": [{"text": "hello", "start": True, "end": 1}]},
    ],
)
def test_malformed_provider_payload_is_an_actionable_value_error(data):
    with pytest.raises(ValueError):
        normalize_response(data, 3)


@pytest.mark.parametrize("value", [True, False, None, [], "bad", float("nan"), float("inf")])
def test_invalid_word_timestamp_values(value):
    data = payload()
    data["words"][0]["start"] = value
    with pytest.raises(ValueError):
        normalize_response(data, 3)


def test_failed_audio_preparation_removes_partial_upload(monkeypatch, tmp_path):
    config = load_config()["whisper"]
    config.update(api_url="https://transcription.example/transcriptions", model="hosted")
    monkeypatch.setenv("WHISPER_API_KEY", "test-only")

    def fail(*args):
        (tmp_path / "transcription.flac").write_bytes(b"incomplete")
        raise Cancelled("Stopped during preparation")

    monkeypatch.setattr("app.transcription.run_ffmpeg", fail)
    with pytest.raises(Cancelled):
        Transcriber(config).transcribe(tmp_path / "input.audio", 3, threading.Event())
    assert not (tmp_path / "transcription.flac").exists()
