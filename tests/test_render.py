import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.captions import layout
from app.config import load_config
from app.footage import probe
from app.timeline import Source, Word, build_plan, sentences_from_words
from app.video_builder import image_motion_filter, render


def command(args):
    subprocess.run(args, check=True, capture_output=True)


def frame(path, t):
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(t),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(result.stdout, np.uint8).reshape(480, 320, 3)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_real_render_progressive_persistent_stable_and_looped(tmp_path, composition_assets):
    c = load_config()
    c["composition"] = composition_assets
    c["video"].update(width=320, height=480, fps=20, preset="ultrafast", crf=16)
    c["captions"].update(font_size=30, safe_margin=20, max_words_per_line=3)
    audio, source = tmp_path / "voice.wav", tmp_path / "source.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            str(audio),
        ]
    )
    # A red box outside the caption area verifies footage coverage after source looping.
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x480:r=20:d=0.5,drawbox=x=10:y=10:w=20:h=20:color=red:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )
    words = [
        Word("Everything", 0.2, 0.5),
        Word("changed", 0.8, 1.0),
        Word("forever.", 1.3, 1.6),
        Word("Yes.", 3, 3.2),
    ]
    plan = build_plan(
        sentences_from_words(words, 4), [Source(str(source), "video", 0.5)], 4, seed=1
    )
    out = render(plan, audio, tmp_path / "out.mp4", tmp_path / "work", c)
    info = probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (320, 480, "20/1")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - 4) < 0.05
    frames = [frame(out, t) for t in [0.1, 0.4, 1.0, 1.7, 2.7, 3.4, 3.9]]
    masks = [(f[80:400, :, :].min(axis=2) > 170) for f in frames]
    assert not masks[0].any(), "No caption before speech"
    assert 20 < masks[1].sum() < masks[2].sum() < masks[3].sum(), "Words appear progressively"
    # Previously visible pixels remain at the same coordinates as words are added.
    assert (masks[1] & masks[2]).sum() / masks[1].sum() > 0.95
    assert (masks[2] & masks[3]).sum() / masks[2].sum() > 0.95
    assert np.mean(masks[3] == masks[4]) > 0.999, "Caption persists during silence"
    assert masks[5].any() and np.mean(masks[5] == masks[6]) > 0.999, "Final caption persists"
    assert masks[5].sum() < masks[3].sum(), "Next sentence replaces prior caption"
    assert all(f[15, 15, 0] > 150 for f in frames), "Looped footage fills the entire output"
    size, lines = layout(words[:3], c["captions"], c["video"])
    assert size > 0 and sorted(i for line in lines for i in line) == [0, 1, 2]


def test_image_and_non_frame_aligned_duration(tmp_path):
    c = load_config()
    c["video"].update(width=320, height=480, fps=20, preset="ultrafast")
    c["captions"].update(font_size=30, safe_margin=20)
    image = tmp_path / "image.png"
    Image.new("RGB", (80, 60), "blue").save(image)
    audio = tmp_path / "audio.wav"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1.013", str(audio)])
    plan = build_plan([], [Source(str(image), "image")], 1.013)
    out = render(plan, audio, tmp_path / "out.mp4", tmp_path / "work", c)
    assert abs(float(probe(out)["format"]["duration"]) - 1.013) < 0.05


def audio_samples(path, channels=1):
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ar",
            "48000",
            "-ac",
            str(channels),
            "-f",
            "f32le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    return np.frombuffer(result.stdout, "<f4").reshape(-1, channels)


def tone_amplitude(samples, frequency):
    # Ignore AAC boundaries and use an integer number of cycles for each tone.
    signal = samples[9600:38400, 0]
    clock = np.arange(len(signal)) / 48000
    return 2 * abs(np.dot(signal, np.exp(-2j * np.pi * frequency * clock))) / len(signal)


@pytest.mark.parametrize("voice_channels", [1, 2])
def test_real_mix_gains_difference_overlay_and_short_asset_loops(
    tmp_path, composition_assets, voice_channels
):
    c = load_config()
    c["composition"] = composition_assets
    c["video"].update(width=320, height=480, fps=20, preset="ultrafast", crf=16)
    c["captions"].update(font_size=30, safe_margin=20)
    image, voice = tmp_path / "image.png", tmp_path / "voice.wav"
    Image.new("RGB", (320, 480), (96, 128, 160)).save(image)
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.013",
            "-ac",
            str(voice_channels),
            str(voice),
        ]
    )
    plan = build_plan([], [Source(str(image), "image")], 1.013)
    work = tmp_path / "work"
    out = render(plan, voice, tmp_path / "mixed.mp4", work, c)
    mixed = audio_samples(out, 2)
    original = audio_samples(voice, voice_channels)
    music = audio_samples(Path(composition_assets["music_directory"]) / "tone.wav", 2)
    # Reference music is shorter than the analysis window; its tone amplitude is
    # constant, so estimate it over its own middle rather than zero-padding it.
    music_level = np.sqrt(2) * np.std(music[2400:14400, 0])
    voice_db = 20 * np.log10(tone_amplitude(mixed, 440) / tone_amplitude(original, 440))
    music_db = 20 * np.log10(tone_amplitude(mixed, 1000) / music_level)
    assert voice_db == pytest.approx(3.4, abs=0.35)
    assert music_db == pytest.approx(-12.0, abs=0.35)
    assert tone_amplitude(mixed, 2000) < 0.001, "Overlay soundtrack must not enter the mix"
    assert np.max(np.abs(mixed)) < 1, "Quiet test sources must mix without clipping"
    normalized = frame(work / "segment_0000.mp4", 0.1)[100:140, 100:140].mean(axis=(0, 1))
    assert np.allclose(normalized, (76.8, 102.4, 128), atol=4), (
        "Image brightness must multiply RGB by 0.8 before blending"
    )
    for time in (0.1, 0.9):
        background = frame(out, time)[100:140, 100:140].mean(axis=(0, 1))
        # 30% dimmed image + 70% difference(dimmed image, gray-32 overlay).
        assert np.allclose(background, (54.4, 80, 105.6), atol=6), (
            "Difference overlay must use 70% opacity over the dimmed image"
        )
    assert abs(float(probe(out)["format"]["duration"]) - 1.013) < 0.05
    composition = json.loads((work / "composition.json").read_text())
    assert composition["blend_mode"] == "difference"
    assert composition["image_brightness"] == 0.8 and composition["overlay_opacity"] == 0.7
    assert composition["music_gain_db"] == -12.0 and composition["voice_gain_db"] == 3.4


def test_images_shake_gently_at_a_balanced_speed_without_exposed_borders(
    tmp_path, composition_assets
):
    c = load_config()
    c["composition"] = composition_assets
    c["video"].update(width=320, height=480, fps=20, preset="ultrafast", crf=16)
    c["captions"].update(font_size=30, safe_margin=20)
    image = tmp_path / "image.png"
    pixels = np.full((480, 320, 3), 100, dtype=np.uint8)
    pixels[220:260, 140:180] = 255
    Image.fromarray(pixels).save(image)
    voice = tmp_path / "voice.wav"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=4", str(voice)])
    plan = build_plan([], [Source(str(image), "image")], 4)
    work = tmp_path / "work"
    render(plan, voice, tmp_path / "out.mp4", work, c)
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(work / "segment_0000.mp4"),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    frames = np.frombuffer(raw.stdout, np.uint8).reshape(-1, 480, 320, 3)
    # Intensity-weighted centers retain subpixel movement. A binary white mask
    # can jump when a softened edge crosses its threshold, even during smooth motion.
    centers = []
    for f in frames:
        weights = np.maximum(f.mean(axis=2) - 110, 0)
        mass = weights.sum()
        centers.append(
            [
                np.dot(np.arange(480), weights.sum(axis=1)) / mass,
                np.dot(np.arange(320), weights.sum(axis=0)) / mass,
            ]
        )
    centers = np.array(centers)
    assert 0.4 < np.linalg.norm(centers[-1] - centers[0]) < 4, (
        "Shallow motion replaces static images"
    )
    assert np.max(np.linalg.norm(np.diff(centers, axis=0), axis=1)) < 1.1, "No sudden shaking"
    # Measure the image's reversals: a shot should oscillate rather than drift.
    steps = np.diff(centers[:, 1])
    directions = np.sign(steps[np.abs(steps) > 0.05])
    reversals = np.count_nonzero(directions[1:] != directions[:-1])
    assert reversals >= 3, "Image shake should reverse gently several times within the shot"
    assert np.all(np.ptp(centers, axis=0) < np.array([480, 320]) * 0.012 + 1), (
        "Faster shaking must keep the same small travel distance"
    )
    assert np.min(frames[:, :, [0, -1]]) > 50
    assert np.min(frames[:, [0, -1], :]) > 50, "Overscan must prevent black borders"


def test_each_render_selects_one_random_music_track(tmp_path, composition_assets):
    c = load_config()
    c["composition"] = composition_assets
    c["video"].update(width=320, height=480, fps=20, preset="ultrafast")
    c["captions"].update(font_size=30, safe_margin=20)
    alternate = Path(composition_assets["music_directory"]) / "alternate.wav"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1500:sample_rate=48000:duration=0.4",
            "-ac",
            "2",
            str(alternate),
        ]
    )
    image, voice = tmp_path / "image.png", tmp_path / "voice.wav"
    Image.new("RGB", (320, 480), "blue").save(image)
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(voice)])
    plan = build_plan([], [Source(str(image), "image")], 1)
    choices = []
    for seed, frequency in [(0, 1000), (1, 1500)]:
        c["footage"]["random_seed"] = seed
        work = tmp_path / f"work-{seed}"
        output = render(plan, voice, tmp_path / f"out-{seed}.mp4", work, c)
        choices.append(json.loads((work / "composition.json").read_text())["music"])
        samples = audio_samples(output, 2)
        assert tone_amplitude(samples, frequency) > 0.02
        assert tone_amplitude(samples, 2500 - frequency) < 0.001, "Mix exactly one selected track"
    assert len(set(choices)) == 2


def test_shake_controls_change_real_motion_speed_distance_and_disable(tmp_path):
    image = tmp_path / "marker.png"
    pixels = np.full((240, 160, 3), 100, dtype=np.uint8)
    pixels[100:140, 60:100] = 255
    Image.fromarray(pixels).save(image)

    def motion(**overrides):
        settings = {**load_config()["shake"], "zoom": 1.12, "amplitude_y": 0, **overrides}
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-loop",
                "1",
                "-framerate",
                "20",
                "-i",
                str(image),
                "-vf",
                image_motion_filter(160, 240, 20, 0, settings),
                "-frames:v",
                "80",
                "-filter_threads",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        ).stdout
        frames = np.frombuffer(raw, np.uint8).reshape(-1, 240, 160, 3)
        assert frames[:, :, [0, -1]].min() > 50 and frames[:, [0, -1]].min() > 50
        weights = np.maximum(frames.mean(axis=3) - 110, 0)
        x = (weights.sum(axis=1) * np.arange(160)).sum(axis=1) / weights.sum(axis=(1, 2))
        y = (weights.sum(axis=2) * np.arange(240)).sum(axis=1) / weights.sum(axis=(1, 2))
        assert np.ptp(y) < 0.01, "Zero vertical amplitude must hold the vertical position"
        return x

    small = motion(amplitude_x=0.2, period_x=2)
    large = motion(amplitude_x=0.8, period_x=2)
    fast = motion(amplitude_x=0.8, period_x=1)
    still = motion(enabled=False)
    assert np.ptp(large) > 3 * np.ptp(small)
    assert np.ptp(still) < 0.01

    def reversals(x):
        steps = np.diff(x)
        signs = np.sign(steps[np.abs(steps) > 0.03])
        return np.count_nonzero(signs[1:] != signs[:-1])

    assert reversals(fast) >= reversals(large) + 3
