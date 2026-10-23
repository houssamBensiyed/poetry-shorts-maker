"""Plan footage cuts and persistent captions using a shared audio clock.

This module performs no I/O. Sentence starts above a word threshold determine
footage changes; individual word starts determine caption reveals. Separating
those rules keeps short sentences on the current footage and avoids changes
at every caption update. A supplied random seed makes source selection repeatable.
"""

import math
import random
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Word:
    """Store one recognized token and its start/end seconds from audio start.

    Text may include restored punctuation. Construction is permissive; call
    validate_words before relying on ordering, finiteness or duration bounds.
    """

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Sentence:
    """Group a nonempty tuple of timestamped words into one caption unit.

    Sentence grouping follows punctuation rather than transcription API segments.
    Empty tuples are unsupported because start reads the first word directly.
    """

    words: tuple[Word, ...]

    @property
    def start(self):
        """Return the first word's start time, which anchors the sentence and caption."""
        return self.words[0].start

    @property
    def text(self):
        """Join tokens with spaces for a readable sentence without changing timings."""
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Source:
    """Describe a candidate or resolved piece of visual media.

    Attributes:
        path: Remote URL before resolution, or local renderable path afterward.
        kind: image or video; selected by the configured footage mode.
        duration: Positive video duration in seconds; unused and normally zero
            for still images, which can fill any planned interval.
    """

    path: str
    kind: str
    duration: float = 0


@dataclass(frozen=True)
class Interval:
    """Place a source on the output timeline from start to end seconds.

    offset is a starting position inside a video source, independent of the
    output start time; still images use zero. Adjacent intervals share boundaries
    so validated footage covers the full audio with no gaps or overlaps.
    """

    start: float
    end: float
    source: Source
    offset: float


@dataclass(frozen=True)
class Caption:
    """Keep a sentence visible from its first word until end seconds.

    End normally equals the next sentence's start or the audio duration. This
    holds the last revealed text through silence rather than clearing captions
    immediately when the final word finishes.
    """

    sentence: Sentence
    end: float

    def reveals(self):
        """Return (start_seconds, cumulative_text) pairs for successive word reveals.

        These values express reveal timing; the ASS writer separately keeps hidden
        words in the full layout so earlier text does not move between events.
        """
        return [
            (w.start, " ".join(x.text for x in self.sentence.words[: i + 1]))
            for i, w in enumerate(self.sentence.words)
        ]


@dataclass(frozen=True)
class Plan:
    """Hold the complete voiceover duration, visual intervals and caption units.

    Frozen tuple-based values make planning independent of rendering. Creation
    alone does not enforce consistency; build_plan validates its result.
    """

    duration: float
    footage: tuple[Interval, ...]
    captions: tuple[Caption, ...]

    def to_dict(self):
        """Recursively convert timeline dataclasses to checkpoint-serializable values."""
        return asdict(self)


def validate_words(words, duration):
    """Reject invalid audio duration, empty tokens or unsafe word timestamps.

    duration must be positive and finite. Each word must have finite ordered
    bounds within it, and both starts and ends must be nondecreasing across
    words. Individual words may overlap; cross-sentence persistence is checked
    later by validate_plan. Empty speech is valid for a positive-duration audio.

    Raises:
        ValueError: A duration, text or timing invariant is violated.
    """
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
    """Group validated words by ending punctuation across API segment boundaries.

    Args:
        words: Timestamped tokens, including punctuation restored by the adapter.
        duration: Original audio duration in seconds, used for validation.
        fallback_words: Positive chunk size for remaining unpunctuated text.

    Return Sentence values. Recognize Western and common CJK terminators with
    optional closing quotes/brackets. Chunk only the trailing unfinished text;
    never split an already punctuated sentence solely because it is long.
    """
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


def build_plan(sentences, sources, duration, threshold=3, seed=None, avoid_repeat=True, mode=None):
    """Choose visual intervals and caption lifetimes covering the original audio.

    Args:
        sentences: Ordered, nonempty Sentence groups; an empty sequence is valid.
        sources: Nonempty image/video candidate sequence.
        duration: Positive audio duration in seconds.
        threshold: Minimum sentence word count that starts a new visual selection.
        seed: Optional local random-generator seed for reproducible choices.
        avoid_repeat: Exclude the previous source when another candidate exists.
        mode: image/video filters candidates; hybrid requires both and opens with video.
            None keeps the supplied catalog unchanged for custom integrations.

    Start footage at zero, cut only at qualifying sentence starts, and keep short
    sentences on the current source. Video offsets are randomly chosen within
    available duration, while short videos can later be looped by the renderer.
    Captions persist to the next sentence or audio end. Return a validated Plan;
    invalid timing, sources, thresholds or caption overlap raise ValueError.
    """
    words = [w for s in sentences for w in s.words]
    validate_words(words, duration)
    if mode is not None:
        if mode not in ("image", "video", "hybrid"):
            raise ValueError("Footage mode must be image, video or hybrid")
        required = {"image", "video"} if mode == "hybrid" else {mode}
        sources = [s for s in sources if s.kind in required]
        if not required.issubset({s.kind for s in sources}):
            raise ValueError(f"{mode} mode requires {' and '.join(sorted(required))} sources")
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
        if mode == "hybrid" and not intervals:
            choices = [x for x in choices if x.kind == "video"]
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
    """Check full visual coverage, exact caption words and sentence-based cuts.

    Args:
        plan: Timeline whose duration and intervals must agree.
        words: Expected transcription tokens in their original order.
        threshold: Same qualifying-sentence word count used by the planner.

    Raise ValueError for footage gaps/overlaps, lost or changed caption words,
    invalid caption persistence, overlapping sentences, or unexpected cut points.
    The renderer repeats these checks before expensive encoding so injected plans
    must honor the same timing contract as the default planner.
    """
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
