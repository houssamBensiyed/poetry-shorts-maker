import shutil
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image
import pytest
from app.config import load_config
from app.timeline import Word, Source, sentences_from_words, build_plan
from app.captions import layout
from app.video_builder import render
from app.footage import probe


def command(args):
    subprocess.run(args, check=True, capture_output=True)


def frame(path, t):
    result = subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(path),'-frames:v','1',
                             '-f','rawvideo','-pix_fmt','rgb24','pipe:1'],check=True,capture_output=True)
    return np.frombuffer(result.stdout,np.uint8).reshape(480,320,3)


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg is not installed')
def test_real_render_progressive_persistent_stable_and_looped(tmp_path):
    c = load_config()
    c['video'].update(width=320,height=480,fps=20,preset='ultrafast',crf=16)
    c['captions'].update(font_size=30,safe_margin=20,max_words_per_line=3)
    audio, source = tmp_path/'voice.wav', tmp_path/'source.mp4'
    command(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4',str(audio)])
    # A red box outside the caption area verifies footage coverage after source looping.
    command(['ffmpeg','-v','error','-y','-f','lavfi','-i',
             "color=c=black:s=320x480:r=20:d=0.5,drawbox=x=10:y=10:w=20:h=20:color=red:t=fill",
             '-c:v','libx264','-pix_fmt','yuv420p',str(source)])
    words = [Word('Everything',.2,.5),Word('changed',.8,1.0),Word('forever.',1.3,1.6),Word('Yes.',3,3.2)]
    plan = build_plan(sentences_from_words(words,4),[Source(str(source),'video',.5)],4,seed=1)
    out = render(plan,audio,tmp_path/'out.mp4',tmp_path/'work',c)
    info = probe(out)
    video = next(s for s in info['streams'] if s['codec_type']=='video')
    assert (video['width'],video['height'],video['r_frame_rate']) == (320,480,'20/1')
    assert any(s['codec_type']=='audio' for s in info['streams'])
    assert abs(float(info['format']['duration'])-4)<.05
    frames = [frame(out,t) for t in [.1,.4,1.0,1.7,2.7,3.4,3.9]]
    masks = [(f[80:400,:,:].min(axis=2)>170) for f in frames]
    assert not masks[0].any(), 'No caption before speech'
    assert 20 < masks[1].sum() < masks[2].sum() < masks[3].sum(), 'Words appear progressively'
    # Previously visible pixels remain at the same coordinates as words are added.
    assert (masks[1]&masks[2]).sum()/masks[1].sum()>.95
    assert (masks[2]&masks[3]).sum()/masks[2].sum()>.95
    assert np.mean(masks[3] == masks[4])>.999, 'Caption persists during silence'
    assert masks[5].any() and np.mean(masks[5] == masks[6])>.999, 'Final caption persists'
    assert masks[5].sum() < masks[3].sum(), 'Next sentence replaces prior caption'
    assert all(f[15,15,0]>150 for f in frames), 'Looped footage fills the entire output'
    size,lines = layout(words[:3],c['captions'],c['video'])
    assert size > 0 and sorted(i for line in lines for i in line)==[0,1,2]


def test_image_and_non_frame_aligned_duration(tmp_path):
    c = load_config()
    c['video'].update(width=320,height=480,fps=20,preset='ultrafast')
    c['captions'].update(font_size=30,safe_margin=20)
    image = tmp_path/'image.png'
    Image.new('RGB',(80,60),'blue').save(image)
    audio = tmp_path/'audio.wav'
    command(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=duration=1.013',str(audio)])
    plan = build_plan([], [Source(str(image),'image')],1.013)
    out = render(plan,audio,tmp_path/'out.mp4',tmp_path/'work',c)
    assert abs(float(probe(out)['format']['duration'])-1.013)<.05
