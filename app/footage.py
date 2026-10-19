import json
import logging
from pathlib import Path
import subprocess
from app.timeline import Source

log = logging.getLogger('video_builder')


def probe(path):
    result = subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],
                            capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise ValueError(f'FFprobe could not read {Path(path).name}: {result.stderr[-1500:]}')
    return json.loads(result.stdout)


def audio_duration(path):
    data = probe(path)
    audio = next((s for s in data['streams'] if s['codec_type']=='audio'),None)
    if not audio:
        raise ValueError('Downloaded file has no audio stream')
    duration = float(audio.get('duration') or data['format'].get('duration') or 0)
    if duration <= 0:
        raise ValueError('Audio is empty or has no measurable duration')
    return duration


def discover(config):
    sources = []
    for key, kind, suffixes in [('videos_directory','video',{'.mp4','.mov','.mkv','.webm','.avi','.m4v'}),
                                ('images_directory','image',{'.png','.jpg','.jpeg','.webp','.bmp'})]:
        directory = Path(config[key])
        if not directory.is_dir():
            raise ValueError(f'Footage directory not found: {directory}')
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in suffixes or not path.is_file():
                continue
            try:
                data = probe(path)
                stream = next(s for s in data['streams'] if s['codec_type']=='video')
                duration = float(stream.get('duration') or data['format'].get('duration') or 0)
                if kind=='video' and duration<=0:
                    raise ValueError('Video duration is unknown')
                sources.append(Source(str(path.resolve()),kind,duration))
            except (ValueError,StopIteration,subprocess.TimeoutExpired) as exc:
                log.warning('Skipping corrupted footage %s: %s',path.name,exc)
    if not sources:
        raise ValueError('No usable footage. Add videos or images to the configured folders.')
    return sources
