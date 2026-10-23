import pytest

from app.timeline import Source, Word, build_plan, sentences_from_words

SOURCES = [Source("a.mp4", "video", 2), Source("b.png", "image")]


def make(texts, starts=None, duration=15):
    starts = starts or [i * 2 for i in range(len(texts))]
    words = [
        Word(w, start + j * 0.2, start + j * 0.2 + 0.1)
        for text, start in zip(texts, starts, strict=True)
        for j, w in enumerate(text.split())
    ]
    sentences = sentences_from_words(words, duration)
    return build_plan(sentences, SOURCES, duration, seed=42)


def test_short_sentences():
    p = make(["I had a dream.", "But.", "One day.", "Everything changed forever."])
    assert [(i.start, i.end) for i in p.footage] == [(0, 6), (6, 15)]
    assert len(p.captions) == 4


def test_leading_short():
    assert [i.start for i in make(["Well.", "One day.", "I realized the truth."]).footage] == [0, 4]


def test_no_qualifying():
    p = make(["Yes.", "No.", "Not yet."])
    assert len(p.footage) == 1
    assert p.footage[0].end == 15
    assert len(p.captions) == 3


def test_silence_and_final():
    p = make(["Hello there.", "Good day."], [0, 4], 8)
    assert [c.end for c in p.captions] == [4, 8]


def test_progressive():
    s = sentences_from_words(
        [Word("Everything", 0, 0.4), Word("changed", 0.5, 0.9), Word("forever.", 1, 1.2)], 2
    )
    p = build_plan(s, SOURCES, 2)
    assert p.captions[0].reveals() == [
        (0, "Everything"),
        (0.5, "Everything changed"),
        (1, "Everything changed forever."),
    ]


def test_repeat_seed_coverage_offsets():
    p = make(["One two three."] * 6)
    assert p == make(["One two three."] * 6)
    assert all(
        a.source != b.source and a.end == b.start
        for a, b in zip(p.footage, p.footage[1:], strict=False)
    )
    assert p.footage[0].start == 0 and p.footage[-1].end == 15
    assert all(i.offset == 0 for i in p.footage)


def test_single_source_and_silence():
    p = build_plan([], SOURCES[:1], 3)
    assert len(p.footage) == 1 and not p.captions


def test_sentence_grouping_ignores_segments():
    words = [
        Word(w, i * 0.1, i * 0.1 + 0.05)
        for i, w in enumerate("I had a dream but one day everything changed.".split())
    ]
    assert len(sentences_from_words(words, 3)) == 1
    assert len(sentences_from_words(words, 3, 3)) == 1
    unpunctuated = [Word(w.text.rstrip("."), w.start, w.end) for w in words]
    assert len(sentences_from_words(unpunctuated, 3, 3)) == 3


@pytest.mark.parametrize(
    "words", [[Word("bad", -1, 1)], [Word("bad", 1, 4)], [Word("bad", float("nan"), 1)]]
)
def test_invalid_timestamps(words):
    with pytest.raises(ValueError):
        sentences_from_words(words, 3)


def test_long_punctuated_sentence_is_not_split_by_fallback():
    words = [Word("word" if i < 29 else "word.", i * 0.1, i * 0.1 + 0.05) for i in range(30)]
    sentences = sentences_from_words(words, 4)
    assert len(sentences) == 1
    plan = build_plan(sentences, SOURCES, 4)
    assert len(plan.footage) == 1 and len(plan.captions) == 1
    assert len(plan.captions[0].sentence.words) == 30


@pytest.mark.parametrize("mode", ["image", "video", "hybrid"])
def test_modes_keep_sentence_cuts_and_randomize_without_immediate_repeats(mode):
    sources = SOURCES + [Source("c.mp4", "video", 5), Source("d.png", "image")]
    sentences = sentences_from_words(
        [
            Word(word, start + j * 0.1, start + j * 0.1 + 0.05)
            for start, text in [
                (0, "One two three."),
                (1, "Short."),
                (2, "Four five six."),
                (4, "Seven eight nine."),
            ]
            for j, word in enumerate(text.split())
        ],
        6,
    )
    selections = set()
    for seed in range(20):
        plan = build_plan(sentences, sources, 6, seed=seed, mode=mode)
        assert plan == build_plan(sentences, sources, 6, seed=seed, mode=mode)
        assert [i.start for i in plan.footage] == [0, 2, 4]
        assert all(
            a.source != b.source for a, b in zip(plan.footage, plan.footage[1:], strict=False)
        )
        kinds = {i.source.kind for i in plan.footage}
        if mode == "hybrid":
            assert plan.footage[0].source.kind == "video"
        else:
            assert kinds == {mode}
        for interval in plan.footage:
            assert (
                0
                <= interval.offset
                <= max(0, interval.source.duration - (interval.end - interval.start))
            )
        selections.add(tuple(i.source.path for i in plan.footage))
    assert len(selections) > 1


def test_hybrid_starts_with_video_even_without_speech_or_later_cuts():
    for seed in range(10):
        plan = build_plan([], SOURCES, 3, seed=seed, mode="hybrid")
        assert len(plan.footage) == 1 and plan.footage[0].source.kind == "video"
    with pytest.raises(ValueError, match="requires image and video"):
        build_plan([], SOURCES[:1], 3, mode="hybrid")
    with pytest.raises(ValueError, match="requires video"):
        build_plan([], SOURCES[1:], 3, mode="video")
