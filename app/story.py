"""Story section parsing, ordered speech alignment, and deterministic frame planning."""

import json
import math
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image

from app.cancellation import check_cancel
from app.config import check_transcription_config
from app.ffmpeg import run_ffmpeg
from app.footage import audio_duration, validate_output
from app.timeline import Interval, Plan, Source, Transition, validate_plan, validate_words
from app.transcription import Transcriber


@dataclass(frozen=True)
class StoryRequest:
    audio: Path
    script: str
    frames: dict[int, Path]
    output: Path
    work: Path


def parse_lines(script):
    lines = [
        {"index": i, "content": text}
        for i, text in enumerate((s.strip() for s in script.splitlines() if s.strip()), 1)
    ]
    if not lines or len(lines) > 200:
        raise ValueError("A story section requires 1–200 nonempty script lines")
    return lines


def numbered_frames(paths):
    result = {}
    for path in paths:
        path = Path(path)
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            continue
        if not re.fullmatch(r"[1-9][0-9]*", path.stem):
            raise ValueError(f"Frame {path.name} must use a positive index, such as 1.png")
        index = int(path.stem)
        if index in result:
            raise ValueError(f"Duplicate frame index {index}")
        result[index] = path
    return result


def section_request(directory, output, work):
    directory = Path(directory).resolve()
    audio = [
        p
        for p in directory.glob("audio.*")
        if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".opus"}
    ]
    if len(audio) != 1:
        raise ValueError("Story section must contain exactly one audio.* recording")
    return StoryRequest(
        audio[0],
        (directory / "script.txt").read_text(encoding="utf-8"),
        numbered_frames((directory / "frames").glob("*")),
        Path(output),
        Path(work),
    )


def validate_inputs(request):
    lines = parse_lines(request.script)
    expected = {line["index"] for line in lines}
    if set(request.frames) != expected:
        raise ValueError(
            f"Frame indexes must match script lines; missing {sorted(expected - request.frames.keys())}, extra {sorted(request.frames.keys() - expected)}"
        )
    for path in request.frames.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Invalid frame: {path.name}")
        with Image.open(path) as image:
            if getattr(image, "is_animated", False):
                raise ValueError("Story frames must be still images")
            image.verify()
    return lines


def tokens(text):
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def align_lines(lines, words, duration):
    """Globally align in script order; reject uncertain boundaries instead of guessing."""
    validate_words(words, duration)
    script, ranges = [], []
    for line in lines:
        start = len(script)
        script.extend(tokens(line["content"]))
        if len(script) == start:
            raise ValueError(f"Line {line['index']} has no spoken words")
        ranges.append((start, len(script)))
    spoken, times = [], []
    for word in words:
        for token in tokens(word.text):
            spoken.append(token)
            times.append((word.start, word.end))
    if max(len(script), len(spoken)) > 10000:
        raise ValueError("Split long scripts into sections of at most 10000 words")
    mapping = {}
    for tag, a, b, c, d in SequenceMatcher(None, script, spoken, autojunk=False).get_opcodes():
        if tag == "equal":
            mapping.update(zip(range(a, b), range(c, d), strict=True))
        elif tag == "replace" and b - a == d - c:
            for i, j in zip(range(a, b), range(c, d), strict=True):
                if SequenceMatcher(None, script[i], spoken[j]).ratio() >= 0.7:
                    mapping[i] = j
    result = []
    for line, (a, b) in zip(lines, ranges, strict=True):
        score = sum(i in mapping for i in range(a, b)) / (b - a)
        valid = score >= 0.7 and a in mapping and b - 1 in mapping
        start = times[mapping[a]][0] if a in mapping else None
        end = times[mapping[b - 1]][1] if b - 1 in mapping else None
        previous_start = result[-1]["start"] if result else None
        if valid and (end <= start or (previous_start is not None and start <= previous_start)):
            valid = False
        result.append({**line, "start": start, "end": end, "confidence": score, "matched": valid})
    return result


def run_story(request, config, stop, report, transcriber=None):
    """Render one section. Inputs and diagnostic files are retained on all outcomes."""
    lines = validate_inputs(request)
    check_cancel(stop)
    request.work.mkdir(parents=True, exist_ok=False)

    def save(name, data):
        (request.work / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save("lines.json", {"lines": lines})
    duration = audio_duration(request.audio)
    if transcriber is None:
        check_transcription_config(config)
        transcriber = Transcriber(config["whisper"])
    report(stage="Transcribing story")
    transcript = transcriber.transcribe(request.audio, duration, stop, request.work)
    save("transcript.json", asdict(transcript))
    check_cancel(stop)
    alignment = align_lines(lines, transcript.words, duration)
    save("alignment.json", {"lines": alignment})
    failed = [line["index"] for line in alignment if not line["matched"]]
    if failed:
        raise ValueError(f"Uncertain alignment for lines {failed}; inspect alignment.json")
    report(stage="Rendering story")
    return render_story(request, alignment, duration, config, stop, report)


def story_plan(request, alignment, duration, fps):
    """Build a provider-independent timeline on frame boundaries before encoding."""
    total = math.ceil(duration * fps - 1e-8)
    if total < 2:
        raise ValueError("Story audio must span at least two video frames")
    starts = [0] + [round(line["start"] * fps) for line in alignment[1:]] + [total]
    if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
        raise ValueError("Story lines are too close to resolve at the configured frame rate")
    intervals, transitions = [], []
    fade = min(0.4, (total - 1) / fps / 2)
    transitions.extend(
        [
            Transition("fade_in", 0, fade),
            Transition("fade_out", (total - 1) / fps - fade, (total - 1) / fps),
        ]
    )
    for i, (start, end) in enumerate(zip(starts, starts[1:], strict=False)):
        intervals.append(
            Interval(
                start / fps, end / fps, Source(str(request.frames[i + 1].resolve()), "image"), 0
            )
        )
        length = (
            min(round(0.4 * fps), (end - start) // 2, (starts[i + 2] - end) // 2)
            if i + 2 < len(starts)
            else 0
        )
        if length:
            transitions.append(Transition("dissolve", (end - length) / fps, end / fps))
    plan = Plan(total / fps, tuple(intervals), (), tuple(transitions))
    validate_plan(plan, [], threshold=None)
    return plan


def render_story(request, alignment, duration, config, stop, report):
    """Bound memory by encoding holds and dissolves separately, then concatenate.

    Every dissolve ends at its new line's start. Transition frames are part of
    the existing timeline, so they do not shorten or shift narration.
    """
    video = config["video"]
    fps, width, height = video["fps"], video["width"], video["height"]
    plan = story_plan(request, alignment, duration, fps)
    (request.work / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2))
    total = round(plan.duration * fps)
    pieces = []
    scale = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,format=yuv420p"
    # Normalize images once. Subsequent encoders only see bounded-size inputs.
    for index, source in request.frames.items():
        run_ffmpeg(
            [
                "-i",
                str(source.resolve()),
                "-vf",
                scale,
                "-frames:v",
                "1",
                "-threads",
                "1",
                f"frame-{index}.png",
            ],
            request.work,
            stop,
            1,
            lambda _: None,
            f"Frame {index}",
        )
    for i, interval in enumerate(plan.footage):
        start, end = round(interval.start * fps), round(interval.end * fps)
        transition = next(
            (
                round((t.end - t.start) * fps)
                for t in plan.transitions
                if t.kind == "dissolve" and t.end == interval.end
            ),
            0,
        )
        segments = [(end - start - transition, False)] + (
            [(transition, True)] if transition else []
        )
        for frames, dissolve in segments:
            name = f"piece-{len(pieces):05d}.mp4"
            args = ["-loop", "1", "-framerate", str(fps), "-i", f"frame-{i + 1}.png"]
            if dissolve:
                args += [
                    "-loop",
                    "1",
                    "-framerate",
                    str(fps),
                    "-i",
                    f"frame-{i + 2}.png",
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    f"[0:v][1:v]blend=all_expr='A*(1-N/{frames})+B*N/{frames}'[v]",
                    "-map",
                    "[v]",
                ]
            args += [
                "-frames:v",
                str(frames),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                video["preset"],
                "-crf",
                str(video["crf"]),
                "-pix_fmt",
                "yuv420p",
                "-threads",
                str(video["threads"]),
                name,
            ]
            run_ffmpeg(
                args, request.work, stop, frames / fps, lambda _: None, f"Story piece {len(pieces)}"
            )
            pieces.append(name)
        report(render_progress=80 * (i + 1) / len(alignment))
    (request.work / "concat.txt").write_text("".join(f"file '{name}'\n" for name in pieces))
    request.output.parent.mkdir(parents=True, exist_ok=True)
    partial = request.output.with_suffix(".partial.mp4")
    fade = plan.transitions[0].end
    try:
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                "concat.txt",
                "-i",
                str(request.audio.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                f"fade=t=in:st=0:d={fade},fade=t=out:st={max(0, (total - 1) / fps - fade)}:d={fade}",
                "-t",
                str(total / fps),
                "-c:v",
                "libx264",
                "-preset",
                video["preset"],
                "-crf",
                str(video["crf"]),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-threads",
                str(video["threads"]),
                "-movflags",
                "+faststart",
                str(partial.resolve()),
            ],
            request.work,
            stop,
            duration,
            lambda p: report(render_progress=80 + p / 5),
            "Story final",
        )
        validate_output(partial, duration, video)
        check_cancel(stop)
        partial.replace(request.output)
        return request.output
    finally:
        partial.unlink(missing_ok=True)
