"""Pure deterministic planning: captions never drive footage cuts."""

import math
import random
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Sentence:
    words: tuple[Word, ...]

    @property
    def start(self):
        return self.words[0].start

    @property
    def text(self):
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Source:
    path: str
    kind: str
    duration: float = 0


@dataclass(frozen=True)
class Interval:
    start: float
    end: float
    source: Source
    offset: float


@dataclass(frozen=True)
class Caption:
    sentence: Sentence
    end: float

    def reveals(self):
        return [
            (w.start, " ".join(x.text for x in self.sentence.words[: i + 1]))
            for i, w in enumerate(self.sentence.words)
        ]


@dataclass(frozen=True)
class Plan:
    duration: float
    footage: tuple[Interval, ...]
    captions: tuple[Caption, ...]

    def to_dict(self):
        return asdict(self)


def validate_words(words, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio duration must be positive and finite")
    previous_start = previous_end = 0.0
    for w in words:
        if not w.text.strip() or not all(math.isfinite(t) for t in (w.start, w.end)):
            raise ValueError("Empty word or missing/invalid word timestamps")
        if (
            not (0 <= w.start <= w.end <= duration)
            or w.start < previous_start
            or w.end < previous_end
        ):
            raise ValueError("Word timestamps must be ordered and inside the audio duration")
        previous_start, previous_end = w.start, w.end


def sentences_from_words(words, duration, fallback_words=20):
    """Punctuation crosses Whisper segments; unpunctuated trailing text uses chunks."""
    validate_words(words, duration)
    if fallback_words < 1:
        raise ValueError("fallback_words must be positive")
    result, current = [], []
    for w in words:
        current.append(w)
        if re.search(r"[.!?。！？][\"\'”’»)]*$", w.text):
            result.append(Sentence(tuple(current)))
            current = []
    for start in range(0, len(current), fallback_words):
        result.append(Sentence(tuple(current[start : start + fallback_words])))
    return result


def build_plan(sentences, sources, duration, threshold=3, seed=None, avoid_repeat=True):
    words = [w for s in sentences for w in s.words]
    validate_words(words, duration)
    if not sources:
        raise ValueError("No valid footage available")
    for source in sources:
        if source.kind not in {"image", "video"} or not source.path:
            raise ValueError("Invalid footage source")
        if source.kind == "video" and (not math.isfinite(source.duration) or source.duration <= 0):
            raise ValueError("Video source duration must be positive and finite")
    if threshold < 1:
        raise ValueError("Footage threshold must be positive")
    starts = [0.0] + sorted(
        set(s.start for s in sentences if len(s.words) >= threshold and 0 < s.start < duration)
    )
    rng, intervals, previous = random.Random(seed), [], None
    for start, end in zip(starts, starts[1:] + [duration], strict=True):
        choices = [x for x in sources if not avoid_repeat or x != previous] or list(sources)
        source = rng.choice(choices)
        offset = (
            rng.uniform(0, max(0, source.duration - (end - start))) if source.kind == "video" else 0
        )
        intervals.append(Interval(start, end, source, offset))
        previous = source
    captions = tuple(
        Caption(s, sentences[i + 1].start if i + 1 < len(sentences) else duration)
        for i, s in enumerate(sentences)
    )
    plan = Plan(duration, tuple(intervals), captions)
    validate_plan(plan, words, threshold)
    return plan


def validate_plan(plan, words, threshold=3):
    validate_words(words, plan.duration)
    cursor = 0.0
    for interval in plan.footage:
        if interval.start != cursor or interval.end <= interval.start:
            raise ValueError("Footage has a gap, overlap, or empty interval")
        cursor = interval.end
    if cursor != plan.duration:
        raise ValueError("Footage does not cover the entire voiceover")
    actual = [w for c in plan.captions for w in c.sentence.words]
    if actual != list(words):
        raise ValueError("Caption words differ from transcription")
    for i, c in enumerate(plan.captions):
        expected = (
            plan.captions[i + 1].sentence.start if i + 1 < len(plan.captions) else plan.duration
        )
        if c.end != expected or c.sentence.start >= c.end or c.sentence.words[-1].end > c.end:
            raise ValueError("Invalid caption persistence or overlapping sentences")
    expected_cuts = {
        c.sentence.start
        for c in plan.captions
        if len(c.sentence.words) >= threshold and 0 < c.sentence.start < plan.duration
    }
    if {i.start for i in plan.footage[1:]} != expected_cuts:
        raise ValueError("Footage cuts do not match qualifying sentences")
