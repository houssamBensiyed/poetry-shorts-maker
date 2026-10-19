import json
import logging
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
from app.captions import write_ass
from app.timeline import validate_plan
from app.footage import probe

log = logging.getLogger('video_builder')


class Cancelled(Exception):
    pass


def check_cancel(stop):
    if stop.is_set():
        raise Cancelled('Processing cancelled; reset the row to NOT_PROCESSED to retry')


def run_ffmpeg(args, cwd, stop, duration, progress, label):
    check_cancel(stop)
    log.info('%s',label)
    log_path = Path(cwd)/(label.replace(' ','_')+'.log')
    with log_path.open('w') as error_log:
        proc = subprocess.Popen(['ffmpeg','-hide_banner','-nostdin','-y','-progress','pipe:1','-nostats']+args,
                                cwd=cwd,stdout=subprocess.PIPE,stderr=error_log,text=True)
        lines = queue.Queue()
        def read():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read,daemon=True)
        reader.start()
        try:
            while True:
                check_cancel(stop)
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith('out_time_us='):
                    try:
                        progress(min(100,max(0,float(line.split('=',1)[1])/1e6/duration*100)))
                    except ValueError:
                        pass
            code = proc.wait()
            if code:
                tail = log_path.read_text()[-3000:]
                raise RuntimeError(f'{label} failed (exit {code}). {tail}')
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()
            reader.join(timeout=2)
    check_cancel(stop)


def render(plan, audio, output, workdir, config, stop=None, progress=lambda p:None):
    validate_plan(plan,[w for c in plan.captions for w in c.sentence.words],
                  config['footage']['minimum_words_for_new_clip'])
    stop = stop or threading.Event()
    work = Path(workdir).resolve()
    work.mkdir(parents=True,exist_ok=True)
    output = Path(output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    video = config['video']
    fps = video['fps']
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('Install FFmpeg and FFprobe before rendering')
    (work/'plan.json').write_text(json.dumps(plan.to_dict(),indent=2))
    font = Path(config['captions']['font_file'])
    if not font.is_file():
        raise ValueError(f'Caption font is missing: {font}')
    (work/'fonts').mkdir(exist_ok=True)
    shutil.copy2(font,work/'fonts'/font.name)
    write_ass(plan,config['captions'],video,work/'captions.ass')
    total_frames = math.ceil(plan.duration*fps-1e-8)
    segments = []
    for index, interval in enumerate(plan.footage):
        # Quantize shared boundaries once; never round each interval independently.
        start_frame = round(interval.start*fps)
        end_frame = total_frames if index == len(plan.footage)-1 else round(interval.end*fps)
        frames = end_frame-start_frame
        if frames <= 0:
            continue
        name = f'segment_{index:04d}.mp4'
        source = interval.source
        inputs = ['-loop','1','-framerate',str(fps)] if source.kind=='image' else ['-stream_loop','-1','-ss',str(interval.offset)]
        vf = (f'scale={video["width"]}:{video["height"]}:force_original_aspect_ratio=increase,'
              f'crop={video["width"]}:{video["height"]},setsar=1,fps={fps},setpts=PTS-STARTPTS')
        args = inputs+['-i',source.path,'-an','-vf',vf,'-frames:v',str(frames),
                      '-c:v','libx264','-preset',video['preset'],'-crf',str(video['crf']),
                      '-pix_fmt','yuv420p','-r',str(fps),'-fps_mode','cfr','-threads',str(video['threads']),'-filter_threads','1',name]
        run_ffmpeg(args,work,stop,frames/fps,
                   lambda p,s=start_frame,n=frames:progress(50*(s+n*p/100)/total_frames),f'Normalize {index+1}')
        segments.append(name)
    (work/'concat.txt').write_text(''.join(f"file '{name}'\n" for name in segments))
    partial = output.with_name(output.stem+'.partial.mp4')
    try:
        args = ['-f','concat','-safe','1','-i','concat.txt','-i',str(Path(audio).resolve()),
                '-map','0:v:0','-map','1:a:0','-vf','ass=captions.ass:fontsdir=fonts',
                '-c:v','libx264','-crf',str(video['crf']),'-preset',video['preset'],
                '-pix_fmt','yuv420p','-threads',str(video['threads']),'-filter_threads','1',
                '-c:a','aac','-b:a','192k','-movflags','+faststart',str(partial)]
        run_ffmpeg(args,work,stop,plan.duration,lambda p:progress(50+p/2),'Render captions and voiceover')
        result = probe(partial)
        vs = next(s for s in result['streams'] if s['codec_type']=='video')
        aud = next((s for s in result['streams'] if s['codec_type']=='audio'),None)
        actual_duration = float(result['format']['duration'])
        rate_num, rate_den = map(int,vs['r_frame_rate'].split('/'))
        if (abs(actual_duration-plan.duration)>1/fps+.002 or not aud or
            abs(float(vs['duration'])-plan.duration)>1/fps+.002 or
            rate_num/rate_den != fps or vs['codec_name'] != 'h264' or aud['codec_name'] != 'aac' or
            (vs['width'],vs['height']) != (video['width'],video['height'])):
            raise ValueError('Rendered output failed duration, resolution, or audio validation')
        check_cancel(stop)
        partial.replace(output)
        progress(100)
        return output
    finally:
        partial.unlink(missing_ok=True)
