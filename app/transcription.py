"""Request hosted timestamped transcription and normalize its response.

The provider must accept the configured multipart transcription format and return
text, word timestamps and segment timestamps. No model weights are downloaded.
A temporary mono 16 kHz FLAC gives the API a predictable input format while the
original buffered audio remains the finished video's voiceover.
"""

import logging
import os
from pathlib import Path

import httpx

from app.cancellation import check_cancel
from app.ffmpeg import run_ffmpeg
from app.models import Transcript as Transcript
from app.timeline import Word, validate_words

log = logging.getLogger("video_builder")

# Whisper can place a final word slightly past the end of the recording.
MAX_END_OVERRUN_SECONDS = 0.5


def normalize_response(data, duration):
    """Restore punctuation and bound small overruns to the original audio.

    Args:
        data: Decoded API object containing text, words and segments arrays.
        duration: Positive original audio duration, bounding all timestamps.

    Case-fold and align every recognized letter/digit between full text and word
    tokens before slicing punctuation back into words. Alignment crosses segment
    boundaries so provider segmentation cannot dictate sentence cuts. Validate
    word ordering and segment bounds, then return an immutable Transcript wrapper.
    Clip word and segment ends up to 0.5 seconds past the audio boundary, only
    when they start inside the recording. Check word ordering before clipping
    so normalization cannot hide reversed provider timestamps. Segment
    dictionaries remain available for recovery diagnostics.

    Raise ValueError for mismatched text, missing speech timestamps, invalid
    shapes or out-of-range timing. Empty speech is valid; recognized words require
    segments. Never invent timings to make an incompatible response appear usable.
    """
    validate_words([], duration)
    if not isinstance(data, dict):
        raise ValueError("Invalid transcription API response: expected an object")
    text = data.get("text", "")
    raw = data.get("words", [])
    if raw is None:
        raw = []
    if not isinstance(text, str) or not isinstance(raw, list):
        raise ValueError("Invalid transcription API response")
    if text.strip() and not raw:
        raise ValueError("Transcription API returned speech without word timestamps")
    canonical, positions = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            for normalized in char.casefold():
                canonical.append(normalized)
                positions.append(index)
    tokens = []
    for word in raw:
        if not isinstance(word, dict) or not isinstance(word.get("word"), str):
            raise ValueError("Transcription API returned an invalid word")
        token = "".join(c.casefold() for c in word.get("word", "") if c.isalnum())
        if not token or word.get("start") is None or word.get("end") is None:
            raise ValueError(
                "Transcription API returned an invalid word or missing word timestamps"
            )
        tokens.append(token)
    if "".join(tokens) != "".join(canonical):
        raise ValueError(
            "Transcription text does not align with timestamped words; cannot safely restore punctuation"
        )
    words, cursor, start_char = [], 0, 0
    for word, token in zip(raw, tokens, strict=True):
        cursor += len(token)
        end_char = positions[cursor] if cursor < len(positions) else len(text)
        words.append(
            Word(
                text[start_char:end_char].strip(), timestamp(word["start"]), timestamp(word["end"])
            )
        )
        start_char = end_char
    validate_words(words, duration + MAX_END_OVERRUN_SECONDS)
    words = [Word(w.text, w.start, bounded_end(w.start, w.end, duration)) for w in words]
    validate_words(words, duration)
    segments = []
    raw_segments = data.get("segments", [])
    if raw_segments is None:
        raw_segments = []
    if not isinstance(raw_segments, list):
        raise ValueError("Transcription API returned invalid segments")
    for segment in raw_segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ValueError("Transcription API returned an invalid segment")
        start, end = timestamp(segment.get("start")), timestamp(segment.get("end"))
        end = bounded_end(start, end, duration)
        segments.append({"text": segment["text"], "start": start, "end": end})
    if words and not segments:
        raise ValueError("Transcription API did not return requested segment timestamps")
    return Transcript(tuple(words), tuple(segments))


def bounded_end(start, end, duration):
    """Cap a small end overrun without accepting speech outside the recording."""
    if 0 <= start <= end <= duration:
        return end
    if 0 <= start < duration < end <= duration + MAX_END_OVERRUN_SECONDS:
        return duration
    raise ValueError("API timestamps are outside the audio duration or reversed")


def timestamp(value):
    """Convert a numeric value or numeric string to seconds, rejecting booleans.

    Raise ValueError for unsupported or unconvertible values. Finiteness and
    audio bounds are enforced by the caller, not by this conversion helper.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Transcription API returned an invalid timestamp")
    try:
        return float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Transcription API returned an invalid timestamp") from exc


class Transcriber:
    """Convert buffered audio and call a configured hosted transcription endpoint."""

    def __init__(self, config):
        """Retain provider settings without checking credentials or making requests."""
        self.config = config

    def transcribe(self, path, duration, stop, workdir=None):
        """Return validated speech timing for a local original audio file.

        Args:
            path: Audio file readable by FFmpeg, regardless of filename extension.
            duration: Probed original duration in seconds.
            stop: Cancellation event checked between preparation/request/retry stages.
            workdir: Optional scratch directory; defaults to the audio file's parent.

        Read the API key from the configured environment variable. Prepare mono
        16 kHz FLAC, enforce this adapter's 24,000,000-byte upload ceiling, and request
        verbose JSON with word and segment timing. Retry transport/transient HTTP
        failures up to four total attempts, reopening the file for each request.
        Disable redirects so credentials stay on the configured request endpoint.

        Always remove transcription.flac, retaining the original audio. Conversion,
        configuration, cancellation and response-validation exceptions propagate.
        Retried requests may be billed by a provider even after an ambiguous timeout.
        """
        check_cancel(stop)
        if not self.config["api_url"] or not self.config["model"]:
            raise ValueError(
                "Set whisper.api_url and whisper.model for your transcription provider"
            )
        key = os.environ.get(self.config["api_key_env"], "")
        if not key:
            raise ValueError(
                f"Set {self.config['api_key_env']} in the project's .env or export it "
                "in the environment for transcription, then restart the application"
            )
        # Direct downloads have no reliable extension. A lossless ASR copy also
        # avoids unsupported input containers; the original remains the render audio.
        path = Path(path).resolve()
        work = Path(workdir).resolve() if workdir else path.parent
        work.mkdir(parents=True, exist_ok=True)
        upload = work / "transcription.flac"
        try:
            run_ffmpeg(
                [
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "flac",
                    str(upload),
                ],
                work,
                stop,
                duration,
                lambda _: None,
                "Prepare API audio",
            )
            if upload.stat().st_size > 24_000_000:
                raise ValueError(
                    "Transcription audio exceeds the API upload limit (24 MB); use a shorter voiceover"
                )
            fields = {
                "model": self.config["model"],
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
            }
            if self.config["language"]:
                fields["language"] = self.config["language"]
            log.info("Transcribing with hosted API model %s", self.config["model"])
            with httpx.Client(
                timeout=self.config["timeout_seconds"], follow_redirects=False
            ) as client:
                for attempt in range(4):
                    check_cancel(stop)
                    try:
                        with upload.open("rb") as audio:
                            response = client.post(
                                self.config["api_url"],
                                headers={"Authorization": f"Bearer {key}"},
                                data=fields,
                                files={"file": ("voiceover.flac", audio, "audio/flac")},
                            )
                        if response.status_code == 200:
                            check_cancel(stop)
                            return normalize_response(response.json(), duration)
                        if response.status_code not in {408, 429, 500, 502, 503, 504}:
                            raise ValueError(
                                f"Transcription API returned HTTP {response.status_code}; check endpoint, model, key and account access"
                            )
                        failure = f"HTTP {response.status_code}"
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        failure = type(exc).__name__
                    if attempt == 3:
                        raise RuntimeError(f"Transcription API failed after 4 attempts ({failure})")
                    log.warning("Temporary transcription API failure (%s); retrying", failure)
                    stop.wait(2**attempt)
        finally:
            upload.unlink(missing_ok=True)
