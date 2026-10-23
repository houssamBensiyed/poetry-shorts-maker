"""Render resolved timelines into validated H.264/AAC vertical MP4s.

Normalize each visual interval to a common size/rate, concatenate those segments,
blend the film overlay, burn ASS captions, and mix gained voice/music. Shared
frame boundaries avoid accumulating rounding error between intervals. Only a
probed, validated partial output is atomically renamed to the final destination.
"""

import json
import logging
import math
import random
import shutil
import threading
from pathlib import Path

from app.cancellation import Cancelled as Cancelled
from app.cancellation import check_cancel
from app.captions import write_ass
from app.config import check_render_config, music_candidates
from app.ffmpeg import run_ffmpeg as run_ffmpeg
from app.footage import probe
from app.timeline import validate_plan

log = logging.getLogger("video_builder")
MUSIC_GAIN_DB = -12.0
IMAGE_BRIGHTNESS = 0.8
OVERLAY_OPACITY = 0.7
SHAKE_PERIOD_X = 2.4
SHAKE_PERIOD_Y = 3.2


def image_motion_filter(width, height, fps, start):
    """Use small, smooth oscillations at a moderate pace without black edges.

    Work at twice the output resolution for half-pixel crop steps. Three percent
    overscan leaves room for shallow independent 2.4/3.2-second horizontal/vertical
    cycles. Global timeline time keeps phase consistent across image cuts.
    """
    time = f"(on/{fps}+{start:.9f})"
    return (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},setsar=1,format=gbrp,"
        "zoompan=z=1.03:"
        f"x='(iw-iw/zoom)/2*(1+0.35*sin(2*PI*{time}/{SHAKE_PERIOD_X}))':"
        f"y='(ih-ih/zoom)/2*(1+0.35*sin(2*PI*{time}/{SHAKE_PERIOD_Y}))':"
        f"d=1:s={width}x{height}:fps={fps},format=gbrp,"
        f"lutrgb=r=val*{IMAGE_BRIGHTNESS}:g=val*{IMAGE_BRIGHTNESS}:b=val*{IMAGE_BRIGHTNESS},"
        "setpts=PTS-STARTPTS"
    )


def render(plan, audio, output, workdir, config, stop=None, progress=lambda p: None):
    """Encode a timeline and return the finalized output Path.

    Args:
        plan: Validated timeline with readable local image/video sources.
        audio: Buffered original audio, used instead of the transcription FLAC.
        output: Final MP4 destination; its parent is created when needed.
        workdir: Workspace for plan.json, copied font, ASS, segments and logs.
        config: Full config containing video, captions, footage and composition paths.
        stop: Optional cancellation event; an unset event is created if omitted.
        progress: Non-throwing callback receiving 0..100 across both render phases.

    Revalidate timeline invariants before encoding. Scale/crop sources to fill
    the output, loop images or short videos, and quantize shared cut boundaries
    once. Segment normalization occupies the first half of progress; caption
    difference blend, caption burn-in and audio mixing occupy the second half.
    One random music track is looped/trimmed to the voice duration at -12.0 dB;
    the buffered voice is increased by +3.4 dB, without amix normalization.
    Still-image RGB brightness is multiplied by 0.8 before overlay composition.
    Overlay audio is excluded. Difference blending uses 70% overlay opacity;
    captions are applied afterwards to preserve their brightness and readability.
    Retain intermediates for the
    lifecycle runner to clean only after acknowledged job completion.

    Encode to a sibling .partial.mp4, then probe audio/video presence, H.264/AAC
    codecs, resolution, frame rate and duration within one frame plus 2 ms.
    Check cancellation and report completion before atomically finalizing output.
    Remove the partial file on exit. Raise on invalid plans, missing tools/font,
    encoder/probe failure or cancellation; never raise after successful rename.
    """
    validate_plan(
        plan,
        [w for c in plan.captions for w in c.sentence.words],
        config["footage"]["minimum_words_for_new_clip"],
    )
    stop = stop or threading.Event()
    work = Path(workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    video = config["video"]
    fps = video["fps"]
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Install FFmpeg and FFprobe before rendering")
    check_render_config(config)
    voice_info = next(s for s in probe(audio)["streams"] if s["codec_type"] == "audio")
    # Duplicate mono centrally without the resampler's implicit -3 dB pan law:
    # the requested +3.4 dB gain must hold in either output channel.
    voice_layout = "pan=stereo|c0=c0|c1=c0," if voice_info["channels"] == 1 else ""
    music = random.Random(config["footage"]["random_seed"]).choice(music_candidates(config))
    overlay = Path(config["composition"]["overlay_file"]).resolve()
    (work / "composition.json").write_text(
        json.dumps(
            {
                "music": str(music.resolve()),
                "overlay": str(overlay),
                "music_gain_db": MUSIC_GAIN_DB,
                "voice_gain_db": 3.4,
                "blend_mode": "difference",
                "image_brightness": IMAGE_BRIGHTNESS,
                "overlay_opacity": OVERLAY_OPACITY,
                "image_motion": "gentle_shake",
                "image_motion_periods_seconds": {"x": SHAKE_PERIOD_X, "y": SHAKE_PERIOD_Y},
            },
            indent=2,
        )
    )
    (work / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2))
    font = Path(config["captions"]["font_file"])
    if not font.is_file():
        raise ValueError(f"Caption font is missing: {font}")
    (work / "fonts").mkdir(exist_ok=True)
    shutil.copy2(font, work / "fonts" / font.name)
    write_ass(plan, config["captions"], video, work / "captions.ass")
    total_frames = math.ceil(plan.duration * fps - 1e-8)
    segments = []
    for index, interval in enumerate(plan.footage):
        # Quantize shared boundaries once; never round each interval independently.
        start_frame = round(interval.start * fps)
        end_frame = total_frames if index == len(plan.footage) - 1 else round(interval.end * fps)
        frames = end_frame - start_frame
        if frames <= 0:
            continue
        name = f"segment_{index:04d}.mp4"
        source = interval.source
        inputs = (
            ["-loop", "1", "-framerate", str(fps)]
            if source.kind == "image"
            else ["-stream_loop", "-1", "-ss", str(interval.offset)]
        )
        vf = (
            image_motion_filter(video["width"], video["height"], fps, start_frame / fps)
            if source.kind == "image"
            else (
                f"scale={video['width']}:{video['height']}:force_original_aspect_ratio=increase,"
                f"crop={video['width']}:{video['height']},setsar=1,fps={fps},setpts=PTS-STARTPTS"
            )
        )
        args = inputs + [
            "-i",
            source.path,
            "-an",
            "-vf",
            vf,
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            "-preset",
            video["preset"],
            "-crf",
            str(video["crf"]),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-threads",
            str(video["threads"]),
            "-filter_threads",
            "1",
            name,
        ]
        run_ffmpeg(
            args,
            work,
            stop,
            frames / fps,
            lambda p, s=start_frame, n=frames: progress(50 * (s + n * p / 100) / total_frames),
            f"Normalize {index + 1}",
        )
        segments.append(name)
    (work / "concat.txt").write_text("".join(f"file '{name}'\n" for name in segments))
    partial = output.with_name(output.stem + ".partial.mp4")
    try:
        # Blend RGB channels so difference has its usual compositing semantics.
        # Apply captions afterwards to preserve their layout and readability.
        filters = (
            f"[0:v:0]setpts=PTS-STARTPTS,format=gbrp[base];"
            f"[2:v:0]scale={video['width']}:{video['height']}:"
            "force_original_aspect_ratio=increase,"
            f"crop={video['width']}:{video['height']},setsar=1,fps={fps},"
            "setpts=PTS-STARTPTS,format=gbrp[film];"
            f"[base][film]blend=all_mode=difference:all_opacity={OVERLAY_OPACITY}:shortest=1,"
            "ass=captions.ass:fontsdir=fonts,format=yuv420p[video];"
            "[1:a:0]asetpts=PTS-STARTPTS,volume=3.4dB,"
            f"{voice_layout}"
            "aformat=sample_rates=48000:channel_layouts=stereo[voice];"
            f"[3:a:0]asetpts=PTS-STARTPTS,volume={MUSIC_GAIN_DB:.1f}dB,"
            "aformat=sample_rates=48000:channel_layouts=stereo[music];"
            "[voice][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"atrim=duration={plan.duration:.9f}[audio]"
        )
        args = [
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            "concat.txt",
            "-i",
            str(Path(audio).resolve()),
            "-stream_loop",
            "-1",
            "-i",
            str(overlay),
            "-stream_loop",
            "-1",
            "-i",
            str(music.resolve()),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            filters,
            "-map",
            "[video]",
            "-map",
            "[audio]",
            "-t",
            f"{total_frames / fps:.9f}",
            "-c:v",
            "libx264",
            "-crf",
            str(video["crf"]),
            "-preset",
            video["preset"],
            "-pix_fmt",
            "yuv420p",
            "-threads",
            str(video["threads"]),
            "-filter_threads",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(partial),
        ]
        run_ffmpeg(
            args,
            work,
            stop,
            plan.duration,
            lambda p: progress(50 + p / 2),
            "Compose overlay captions and audio",
        )
        result = probe(partial)
        vs = next(s for s in result["streams"] if s["codec_type"] == "video")
        aud = next((s for s in result["streams"] if s["codec_type"] == "audio"), None)
        actual_duration = float(result["format"]["duration"])
        rate_num, rate_den = map(int, vs["r_frame_rate"].split("/"))
        if (
            abs(actual_duration - plan.duration) > 1 / fps + 0.002
            or not aud
            or abs(float(vs["duration"]) - plan.duration) > 1 / fps + 0.002
            or rate_num / rate_den != fps
            or vs["codec_name"] != "h264"
            or aud["codec_name"] != "aac"
            or (vs["width"], vs["height"]) != (video["width"], video["height"])
        ):
            raise ValueError("Rendered output failed duration, resolution, or audio validation")
        check_cancel(stop)
        progress(100)
        partial.replace(output)
        return output
    finally:
        partial.unlink(missing_ok=True)
