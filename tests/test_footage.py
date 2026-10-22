import pytest

from app.footage import audio_duration


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
